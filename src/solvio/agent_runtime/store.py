"""Das Agent Run Ledger — es fuehrt Handlungen, keine Gedanken (ADR-0029).

Es beantwortet: *Was hast du getan? Welche Agenten haben daran gearbeitet?
Woher kommt dieses Ergebnis? Was laeuft noch? Warum ist das fehlgeschlagen?* —
auch nach einem Absturz.

Es ist ausdruecklich NICHT die Zugriffsspur des Tresors, nicht das
Zahlungsbuch, nicht das Freigabejournal. Wo einer dieser Orte schon eine
Wahrheit fuehrt, **verweist** das Ledger (`approval_id`, `execution_id`,
`call_id`) statt zu kopieren.

Drei Entscheidungen stecken im Code und nicht in einer Ermahnung:

* **Es gibt keine Spalte, in die ein Transkript passt.** Freitext ist einzeln
  benannt (nie `**kwargs`) und laengengedeckelt. Ein Gedankengang hat hier
  keinen Ort — nicht, weil er gefiltert wuerde, sondern weil keiner existiert.
* **Geheimnisgestalt laesst den Schreibvorgang scheitern**, statt bereinigt zu
  werden. Eine Ledger-Zeile ist eine Aussage, und eine halbe Aussage ueber
  etwas, das man nicht zeigen darf, ist schlechter als keine. Erst danach
  laeuft der Text noch durch die Redaktion des Starters — als zweites Netz fuer
  Formen, die das eine Praedikat des Hauses nicht kennt, nicht als Ersatz fuer
  die Verweigerung.
* **Die Zustandsmaschine ist eine geschlossene Tabelle.** Eine verbotene Kante
  wirft. `SUCCEEDED → RUNNING` ist keine Nachlaessigkeit, die ein Aufrufer
  vermeiden soll, sondern ein `LedgerTransitionError`.

Hauskonvention vollstaendig: eigene Datei unter `SOLVIO_STATE_DIR`,
Pfad-Override `SOLVIO_AGENT_RUNS_DB` fuer Tests, Verzeichnis 0700, Datei und
`-wal`/`-shm` 0600 unter enger umask, WAL aus dem Schemakopf, `foreign_keys`
und `busy_timeout` **je Verbindung** (die Lehre des Proactive-Stores: im Schema
wirkt `foreign_keys` genau einmal, naemlich auf der Verbindung, die das Schema
anlegt — danach feuert keine Kaskade mehr), additive Migration, Verbindungen je
Operation mit explizitem `close()`.
"""
from __future__ import annotations

import contextlib
import json
import re
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

DEFAULT_PATH = "~/.solvio/agent_runs.sqlite3"

#: Testschalter. Ein Test schreibt nie in das produktive Buch.
PATH_ENV = "SOLVIO_AGENT_RUNS_DB"

#: Wo die Artefakte liegen — Dateien, nicht Datenbankinhalt.
ARTIFACT_DIRNAME = "agent_runs"


def state_dir() -> str:
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def resolve_path(path: str = "") -> str:
    """Wohin das Buch gehoert. Ausdrueckliches Argument schlaegt Umgebung schlaegt
    Vorgabe — dieselbe Reihenfolge wie beim Buch des Brokers."""
    chosen = path or os.environ.get(PATH_ENV, "")
    if not chosen:
        chosen = os.path.join(state_dir(), "agent_runs.sqlite3")
    return os.path.abspath(os.path.expanduser(chosen))


def artifact_root(run_id: str = "") -> str:
    """`~/.solvio/agent_runs/<run_id>/` — neben dem Buch, nicht darin."""
    base = os.path.join(state_dir(), ARTIFACT_DIRNAME)
    return os.path.join(base, run_id) if run_id else base


# =====================================================================
# Geschlossene Vokabulare
# =====================================================================

#: Zustaende eines Laufs (Architektur §5).
CREATED = "CREATED"
PLANNING = "PLANNING"
RUNNING = "RUNNING"
WAITING_SPECIALIST = "WAITING_SPECIALIST"
WAITING_CAPABILITY = "WAITING_CAPABILITY"
WAITING_APPROVAL = "WAITING_APPROVAL"
WAITING_USER = "WAITING_USER"
VERIFYING = "VERIFYING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
INTERRUPTED = "INTERRUPTED"

#: Endzustaende. Endgueltig, kein Wiedereintritt.
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})

#: Zustaende, in denen ein Lauf **parkt** und deshalb KEINEN Laufzeit-Slot
#: belegt (Architektur §5/§9): ein wartender Lauf verstopft die Laufzeit nicht.
#: Zustaende, in denen ein Lauf WARTET statt zu arbeiten. Sie zaehlen nicht
#: gegen die Parallelitaet, und `reconcile()` laesst sie in Ruhe.
#:
#: `WAITING_CAPABILITY` kam mit der Entwicklungsnaht dazu — und zwar aus
#: demselben Grund wie `WAITING_APPROVAL`: der Vorgang, auf den gewartet wird,
#: ueberlebt den Neustart des Cores. Ein Entwicklungsauftrag steht im
#: Autopilot-Buch; ihn beim Neustart zu unterbrechen hiesse, den Auftrag des
#: Nutzers wegzuwerfen, waehrend die Arbeit daran weiterlaeuft.
PARKED_STATES = frozenset({WAITING_APPROVAL, WAITING_USER, WAITING_CAPABILITY})

#: Die geschlossene Uebergangstabelle. Jede Kante ist Code; was nicht dasteht,
#: wirft. Muster: `payment/intent.py`.
#:
#: **Eine Abweichung vom Wortlaut der Architektur, mit Grund.** Die Tabelle in
#: §5 gibt `INTERRUPTED` eine eingehende Kante nur aus den WAITING_*-Zustaenden
#: und aus VERIFYING. §12 verlangt aber, dass beim Neustart **alle**
#: nicht-terminalen Laeufe INTERRUPTED werden — und ein Absturz trifft einen
#: Lauf fast immer in PLANNING oder RUNNING, also genau dort, wo die Tabelle
#: keine Kante hat. Beides zusammen ist nicht erfuellbar.
#:
#: Aufgeloest wird zugunsten von §12, weil das die SICHERHEITSaussage ist:
#: „nichts wird nach einem Neustart still als Erfolg verbucht". Die Kanten
#: kosten nichts an Strenge — `INTERRUPTED` ist ausschliesslich das Ergebnis der
#: Neustart-Abstimmung, kein Zustand, den die Laufzeit im Betrieb ansteuert, und
#: kein Weg aus einem Endzustand heraus (die drei bleiben leer).
#: **Zweite Abweichung, ebenfalls live erzwungen: jeder nicht-terminale Zustand
#: muss ENDEN koennen.** Die Tabelle gab `CREATED` keine Kante nach `FAILED` —
#: konsequent gedacht, denn ein gerade angenommener Lauf hat noch nichts getan,
#: woran er scheitern koennte. Er hat aber: das Anlegen der Arbeitskopie liegt
#: VOR der Planung. Ein Lauf, dessen Klon misslang, wollte `FAILED` werden, die
#: Tabelle verbot es, der Fehler wurde verschluckt, der Lauf blieb
#: nicht-terminal — und der Takt versuchte es alle zwei Sekunden neu. Gemessen:
#: 297 identische Fehlschlaege, bis von Hand gestoppt wurde.
#:
#: Die Lehre ist nicht „diese eine Kante fehlte", sondern dass eine von Hand
#: gepflegte Tabelle sie wieder verlieren kann. Deshalb steht die Regel jetzt
#: als Regel: `_EXITS` wird JEDEM nicht-terminalen Zustand zugerechnet. Das ist
#: keine Lockerung — es ist die Bedingung dafuer, dass ein Lauf ueberhaupt
#: ehrlich enden kann. Die Endzustaende bleiben leer, und ein Selbstuebergang
#: entsteht dabei nicht (`- {state}`).
_EXITS = frozenset({FAILED, CANCELLED, INTERRUPTED})

_EDGES: dict[str, frozenset[str]] = {
    CREATED: frozenset({PLANNING}),
    PLANNING: frozenset({RUNNING, WAITING_USER, FAILED, CANCELLED, INTERRUPTED}),
    RUNNING: frozenset({WAITING_SPECIALIST, WAITING_CAPABILITY, WAITING_APPROVAL,
                        WAITING_USER, VERIFYING, SUCCEEDED, FAILED, CANCELLED,
                        INTERRUPTED}),
    WAITING_SPECIALIST: frozenset({RUNNING, WAITING_USER, FAILED, CANCELLED, INTERRUPTED}),
    # `WAITING_USER` kam mit der Entwicklungsnaht dazu, und zwar fuer genau
    # eine Lage: die Entwicklung ist FERTIG, aber die Faehigkeit ist noch nicht
    # benutzbar. Das ist kein Fehlschlag — der Autopilot endet bauartbedingt
    # bei Commit und Ref, und Merge, Deploy und Neustart sind
    # Owner-Entscheidungen, die ein Contract nicht einmal erbitten darf.
    # Der Lauf wechselt also die Art seines Wartens: vom Warten auf eine
    # Maschine zum Warten auf einen Menschen. Beides sind Parkzustaende; die
    # Kante lockert nichts, sie benennt einen Uebergang, den es sonst nur als
    # Ausnahme gaebe.
    WAITING_CAPABILITY: frozenset({RUNNING, WAITING_USER, FAILED, CANCELLED,
                                   INTERRUPTED}),
    WAITING_APPROVAL: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    WAITING_USER: frozenset({PLANNING, RUNNING, VERIFYING, FAILED, CANCELLED, INTERRUPTED}),
    # `WAITING_USER` kam mit Objective Execution V1A / FIX 2 dazu, und zwar aus
    # der Pruefung heraus: ein Lauf, der sein Ergebnis hat, aber die
    # Zielerfuellung nicht belegen kann, fragt den einzigen, der sie
    # entscheiden kann. Vorher fehlte diese Kante — der Uebergang warf, der
    # generische Fang machte `capability_failed` daraus, und aus einer ehrlichen
    # Frage wurde ein Fehlschlag. Die Kante lockert nichts: `WAITING_USER` ist
    # ein PARKENDER Zustand. Providergrenzen setzen spaeter gezielt die im
    # Grenzdatensatz gebundene Planungs-/Pruefungsphase fort.
    VERIFYING: frozenset({RUNNING, WAITING_USER, SUCCEEDED, FAILED, CANCELLED,
                          INTERRUPTED}),
    INTERRUPTED: frozenset({RUNNING, FAILED, CANCELLED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

#: Die geschlossene Tabelle: die Kanten oben, plus fuer jeden nicht-terminalen
#: Zustand garantiert der Weg hinaus.
TRANSITIONS: dict[str, frozenset[str]] = {
    state: targets if state in TERMINAL_STATES else (targets | _EXITS) - {state}
    for state, targets in _EDGES.items()
}

ALL_STATES = frozenset(TRANSITIONS)

#: Schrittarten (Architektur §6). Geschlossen.
STEP_KINDS = frozenset({
    "plan", "specialist", "capability", "verify", "user_boundary",
    "knowledge_proposal", "capability_need", "harvest", "summary",
})

#: Schrittzustaende.
STEP_STATES = frozenset({
    "pending", "running", "waiting", "succeeded", "failed", "denied",
    "skipped", "unknown",
})

#: Ereignisarten. Geschlossen — die Chronik liest genau diese.
EVENT_KINDS = frozenset({
    "state_changed", "step_started", "step_finished", "approval_requested",
    "approval_resolved", "boundary_opened", "boundary_resumed", "budget_event",
    "recovered", "notice_sent", "provider_route", "native_progress",
    # N8/C4 §3.1: every helper publication (or refusal) is owner-visible.
    "helper_published",
})

#: Fehlerkategorien. Geschlossen (Ledger-Schema).
FAILURE_CATEGORIES = frozenset({
    "plan_invalid", "specialist_unavailable", "specialist_failed", "quota",
    "capability_failed", "approval_denied", "approval_expired", "policy_denied",
    "recovery_required", "budget_exhausted", "loop_detected", "timeout",
    "interrupted", "workspace_conflict", "cancelled_by_user",
    # Ein Bau-Lauf, der kein Arbeitsergebnis hinterlaesst. Live gelernt: das
    # sah wie ein Erfolg aus und war eine leere Zusage.
    "no_result",
    # Der Planer hat einen strukturell ungueltigen Schritt vorgeschlagen —
    # eine Faehigkeit ohne ihre Pflichtangabe — und nach der Nachplanung
    # denselben noch einmal. Live gelernt: das lief vorher als
    # `budget_exhausted` und verschwieg damit, WER nicht weiterkam.
    "planner_invalid_step",
    # Nach einem Prozessverlust gibt es fuer einen Lauf, der schon gearbeitet
    # hat, keinen wiederherstellbaren Plan mehr — kein Checkpoint, ein
    # unlesbarer, oder einer, den die Planpolicy heute ablehnt. Gemessen am
    # 5.9.2026: solche Laeufe endeten auf SUCCEEDED, weil „kein Plan" wie
    # „Plan zu Ende" gelesen wurde. Ein erfundener Ersatzplan waere die
    # falsche Antwort: er wuerde bereits ausgefuehrte Schritte wiederholen.
    "plan_unrecoverable",
    # Der Plan ist abgearbeitet, ein Ergebnis liegt vor — aber dass es das
    # Owner-Ziel erfuellt, ist NICHT belegt. Gemessen am 5.9.2026: solche
    # Laeufe endeten SUCCEEDED mit Aufgabe `completed`. Der Lauf endet jetzt
    # ohne Erfolg, die Aufgabe bleibt offen, das Ergebnis liegt bei.
    "goal_unverified",
})

#: Zustaende einer wartenden Start-Anfrage. Geschlossen wie alles hier.
START_WAITING = "WAITING"
START_TAKEN = "TAKEN"
START_CLOSED = "CLOSED"
START_STATES = frozenset({START_WAITING, START_TAKEN, START_CLOSED})

#: Aufgabenzustaende.
TASK_ACTIVE = "active"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_STATES = frozenset({TASK_ACTIVE, TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

#: Arten von Artefakten.
#:
#: `evaluation_snapshot` kam mit dem Informationsvertrag dazu und musste eine
#: EIGENE Art sein: `report` wird genau einmal geschrieben und ist danach
#: eingefroren (`_write_report` kehrt bei vorhandener Datei zurueck) und deckelt
#: auf 8 Befunde und 12 Quellen. Als „aktueller Stand der Bewertungseingabe"
#: waere er damit falsch — er aendert sich NICHT, wenn neue Befunde kommen.
#: `action_result` haelt, was der Core nach einer ausgefuehrten Handlung SELBST
#: nachgelesen hat — nicht, was die Faehigkeit darueber gemeldet hat. Er ist der
#: einzige Beleg, mit dem eine Handlungsforderung gedeckt werden kann.
ARTIFACT_KINDS = frozenset({"report", "diff", "test_report", "proposal",
                            "log_excerpt", "evaluation_snapshot",
                            "action_result", "task_input", "extension_candidate", "document_result", "document_receipt",
                            "task_file_input", "task_file_manifest", "file_work_receipt", "file_requirement_binding",
                            "result_file", "result_receipt"})

#: Scopes einer Aufgabe. Strukturell getrennt schon bei der Erzeugung.
SCOPE_RESEARCH = "research"
SCOPE_BUILD = "build"
SCOPE_ACTION = "action"
SCOPE_TASK = "task"
SCOPES = frozenset({SCOPE_RESEARCH, SCOPE_BUILD, SCOPE_ACTION, SCOPE_TASK})

# -- Laengendeckel ------------------------------------------------------------
#
# Sie sind der Grund, warum kein Transkript hineinpasst. Ein Deckel, den man
# erhoehen muss, um ein Transkript abzulegen, ist eine sichtbare Entscheidung —
# genau das ist beabsichtigt.
MAX_OBJECTIVE = 4_000
MAX_RESULT_SUMMARY = 4_000
MAX_STEP_SUMMARY = 600
MAX_EVENT_SUMMARY = 300
MAX_REF = 200
# Eine Providerroute ist ein geschlossenes Metadatenobjekt. Ihre nativen
# Kennungen passen nicht in den Textverweis; JSON darf nie abgeschnitten werden.
# Alle anderen Verweise behalten MAX_REF. Kein Prompt-/Transkriptfeld.
MAX_PROVIDER_ROUTE_REF = 1024
MAX_BOUNDARY_JSON = 4_000
#: Der Fortsetzungspunkt eines Laufs. Groesser als die anderen Deckel, und der
#: Grund steht in `checkpoint.py`: der Satz traegt einen ganzen validierten Plan
#: (`MAX_PLAN_STEPS` Schritte à `MAX_TEXT`) plus Fortschritt. Er ist trotzdem
#: kein Transkriptkanal — die Feldmenge dort ist geschlossen und jedes einzelne
#: Feld ist fuer sich gedeckelt.
MAX_PLAN_CHECKPOINT = 32_000

#: Aufbewahrung: wie bei Konversationen.
RETENTION_SECONDS = 90 * 24 * 3600

#: Ereigniszeilen je Lauf. Aelteste zuerst gepruent.
MAX_EVENTS_PER_RUN = 2_000


class LedgerError(RuntimeError):
    """Basis: etwas am Buch stimmt nicht."""


class LedgerTransitionError(LedgerError):
    """Eine Kante, die es in der Tabelle nicht gibt. Fail-closed."""

    def __init__(self, run_id: str, current: str, wanted: str) -> None:
        super().__init__(f"invalid_transition:{current}->{wanted}")
        self.run_id = run_id
        self.current = current
        self.wanted = wanted


class LedgerVocabularyError(LedgerError):
    """Ein Wort ausserhalb eines geschlossenen Vokabulars."""

    def __init__(self, field_name: str, value: str) -> None:
        super().__init__(f"unknown_{field_name}:{value}")
        self.field_name = field_name
        self.value = value


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id            TEXT PRIMARY KEY,
    objective          TEXT NOT NULL,
    scope              TEXT NOT NULL,
    target_repo        TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    created_origin     TEXT NOT NULL,
    created_principal  TEXT NOT NULL,
    conversation_ref   TEXT NOT NULL DEFAULT '',
    predecessor_ref    TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL,
    budget             TEXT NOT NULL,
    -- Informationsvertrag: die gebundenen Anforderungen. Genau EINMAL
    -- geschrieben, danach nur gelesen. Der Schreibvorgang ist ein
    -- bedingtes UPDATE (siehe `bind_requirements`) und keine Lese-dann-
    -- Schreib-Folge: zwei Taktschritte duerfen sich hier nicht ueberholen.
    requirements       TEXT NOT NULL DEFAULT '',
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id             TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    parent_run_id      TEXT NOT NULL DEFAULT '',
    attempt            INTEGER NOT NULL DEFAULT 1,
    state              TEXT NOT NULL,
    plan_revision      INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL,
    outcome            TEXT NOT NULL DEFAULT '',
    failure_category   TEXT NOT NULL DEFAULT '',
    result_summary     TEXT NOT NULL DEFAULT '',
    boundary           TEXT NOT NULL DEFAULT '',
    workspace_path     TEXT NOT NULL DEFAULT '',
    workspace_repo     TEXT NOT NULL DEFAULT '',
    workspace_branch   TEXT NOT NULL DEFAULT '',
    workspace_base     TEXT NOT NULL DEFAULT '',
    branch_ref         TEXT NOT NULL DEFAULT '',
    tokens_planner     INTEGER NOT NULL DEFAULT 0,
    specialist_seconds REAL NOT NULL DEFAULT 0,
    specialist_count   INTEGER NOT NULL DEFAULT 0,
    provider_wait_seconds REAL NOT NULL DEFAULT 0,
    provider_selection TEXT NOT NULL DEFAULT '',
    -- Objective Execution V1A: der validierte Plan und der Fortschritt darin.
    -- Vorher lebte beides nur im Arbeitsspeicher, und ein Prozessverlust machte
    -- aus „Rest offen" ein „nichts mehr zu tun".
    plan_checkpoint    TEXT NOT NULL DEFAULT '',
    -- FIX 2: die verbrauchten Planer-Aufrufe. Bewusst HIER und nicht im
    -- Fortsetzungspunkt: der entsteht am Ende des Takts, und wer davor stirbt,
    -- bekam den Modellaufruf geschenkt. Diese Spalte wird VOR dem Aufruf
    -- gebunden — sie steht neben `plan_revision` und `tokens_planner`, also
    -- dort, wo die Laufbuchhaltung ohnehin lebt.
    planner_calls      INTEGER NOT NULL DEFAULT 0,
    -- Verbrauchte Bewertungsaufrufe. Eigener Zaehler, damit eine Bewertung
    -- keine Planungskapazitaet frisst; vor dem Dispatch gebunden wie
    -- `planner_calls`.
    assessment_calls   INTEGER NOT NULL DEFAULT 0,
    -- Das letzte inhaltliche Bewertungsurteil, an Aufgabe, Lauf,
    -- Anforderungssatz und Ergebnis-Snapshot gebunden.
    completion_verdict TEXT NOT NULL DEFAULT '',
    -- Die Kennung des Entwicklungsauftrags, an dem dieser Lauf haengt.
    -- Die VORWAERTS-Richtung der Zuordnung; rueckwaerts nennt der Vertrag im
    -- Autopilot-Buch den Lauf. Beide ueberleben den Neustart, weil beide in
    -- einem Buch stehen und nicht im Arbeitsspeicher.
    development_ref    TEXT NOT NULL DEFAULT '',
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_open ON agent_runs(state) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_runs_task ON agent_runs(task_id, created_at);

CREATE TABLE IF NOT EXISTS agent_steps (
    step_id            TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    state              TEXT NOT NULL,
    attempt            INTEGER NOT NULL DEFAULT 1,
    specialist_profile TEXT NOT NULL DEFAULT '',
    specialist_role    TEXT NOT NULL DEFAULT '',
    capability         TEXT NOT NULL DEFAULT '',
    call_id            TEXT NOT NULL DEFAULT '',
    approval_id        TEXT NOT NULL DEFAULT '',
    execution_id       TEXT NOT NULL DEFAULT '',
    dispatch_binding_digest TEXT NOT NULL DEFAULT '',
    dispatch_claimed_at REAL,
    outcome_reason     TEXT NOT NULL DEFAULT '',
    child_pgid         INTEGER NOT NULL DEFAULT 0,
    child_started_at   REAL NOT NULL DEFAULT 0,
    child_executable   TEXT NOT NULL DEFAULT '',
    commit_ref         TEXT NOT NULL DEFAULT '',
    artifact_refs      TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    started_at         REAL,
    finished_at        REAL,
    UNIQUE(run_id, seq, attempt)
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id, seq, attempt);

CREATE TABLE IF NOT EXISTS agent_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    run_id   TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    step_id  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    summary  TEXT NOT NULL,
    ref      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, id);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON agent_artifacts(run_id, created_at);

-- Auftraege, die auf eine Freigabe warten, BEVOR es sie als Lauf gibt.
--
-- Live gefunden, und es war der schwerste Fund dieses Milestones: der Nutzer
-- gab per Face ID frei, und nichts geschah. Die Freigabe ging auf EXPIRED, die
-- Tabelle `execution_attempts` blieb bei null Eintraegen.
--
-- Der Grund ist eine Henne-Ei-Luecke. Ein Lauf, der auf eine Freigabe wartet,
-- hat einen Poller — `_poll_approval` im Takt. Der Auftrag, der den Lauf erst
-- ERZEUGT, hatte keinen: die Anfragekennung stand im Umschlag des Werkzeugs
-- und starb mit dem Gespraechszug.
--
-- Diese Zeile ueberlebt den Zug. Sie ist ausdruecklich KEINE Autoritaet: sie
-- haelt nur, was noetig ist, um denselben Aufruf unveraendert zu wiederholen —
-- und die Herkunft steht dabei, weil sie in den Freigabe-Digest eingeht und
-- niemals neu erfunden werden darf.
CREATE TABLE IF NOT EXISTS pending_starts (
    request_id  TEXT PRIMARY KEY,
    capability  TEXT NOT NULL,
    arguments   TEXT NOT NULL,
    principal   TEXT NOT NULL,
    origin      TEXT NOT NULL,
    commanded   INTEGER NOT NULL,
    state       TEXT NOT NULL,
    -- Der Lauf, der aus dieser Freigabe ENTSTANDEN ist. Leer, solange keiner
    -- entstand — und leer fuer jede Zeile, die aelter ist als diese Spalte:
    -- eine fehlende Verbindung wird nicht aus Text oder Uhrzeit erraten.
    run_id      TEXT NOT NULL DEFAULT '',
    conversation_ref TEXT NOT NULL DEFAULT '',
    delivery_ref TEXT NOT NULL DEFAULT '',
    mail_outcome TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_starts_state
    ON pending_starts(state, created_at);
"""

#: Spalten, die eine BESTEHENDE Ablage nachtraegt. Nur ADDITIV — es wird nie
#: eine Spalte entfernt und nie eine umgeschrieben. `CREATE TABLE IF NOT EXISTS`
#: ruehrt eine vorhandene Tabelle nicht an; ohne diese Wanderung kaeme eine neue
#: Spalte bei niemandem an, der die Datei schon hat.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "agent_runs": {
        # Objective Execution V1A. Eine bestehende Ablage hat die Spalte nicht;
        # `CREATE TABLE IF NOT EXISTS` traegt sie nicht nach. Ohne diese Zeile
        # kaeme der Fortsetzungspunkt bei genau den Laeufen nicht an, die es
        # schon gibt — also bei allen produktiven.
        "plan_checkpoint": "TEXT NOT NULL DEFAULT ''",
        "planner_calls": "INTEGER NOT NULL DEFAULT 0",
        "assessment_calls": "INTEGER NOT NULL DEFAULT 0",
        "completion_verdict": "TEXT NOT NULL DEFAULT ''",
        "development_ref": "TEXT NOT NULL DEFAULT ''",
        "provider_wait_seconds": "REAL NOT NULL DEFAULT 0",
        "provider_selection": "TEXT NOT NULL DEFAULT ''",
        "workspace_repo": "TEXT NOT NULL DEFAULT ''",
        "workspace_branch": "TEXT NOT NULL DEFAULT ''",
        "workspace_base": "TEXT NOT NULL DEFAULT ''",
    },
    "agent_steps": {
        "dispatch_binding_digest": "TEXT NOT NULL DEFAULT ''",
        "dispatch_claimed_at": "REAL",
    },
    "agent_tasks": {
        # Cognitive Router V1: worauf dieser Auftrag aufbaut. Ein VERWEIS auf
        # eine Aufgabe DERSELBEN Konversation, vom Core gesetzt — nie
        # Nutzertext und nie Modelltext.
        "predecessor_ref": "TEXT NOT NULL DEFAULT ''",
        "requirements": "TEXT NOT NULL DEFAULT ''",
    },
    "pending_starts": {
        # Agentenauftraege im Gespraechsweg: die Freigabe, die einen Auftrag
        # erst erzeugt, kennt danach ihren Lauf. Vorher stand die Zuordnung
        # nirgends — ein neues Gespraech konnte den Lauf nicht wiederfinden.
        "run_id": "TEXT NOT NULL DEFAULT ''",
        "conversation_ref": "TEXT NOT NULL DEFAULT ''",
        "delivery_ref": "TEXT NOT NULL DEFAULT ''",
        "mail_outcome": "TEXT NOT NULL DEFAULT ''",
    },
}


def _add_missing_columns(connection) -> None:
    """Traegt fehlende Spalten nach. Additiv, still, und ohne Datenverlust."""
    for table, columns in _ADDED_COLUMNS.items():
        if not columns:
            continue
        try:
            present = {row["name"] for row in
                       connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            continue
        if not present:
            continue
        for name, declaration in columns.items():
            if name in present:
                continue
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            log.info("agent_runtime.column_added", table=table, column=name)


# =====================================================================
# Kennungen — vom Core gepraegt, nie vom Modell
# =====================================================================

def new_task_id() -> str:
    return "at-" + secrets.token_hex(8)


def new_run_id() -> str:
    return "ar-" + secrets.token_hex(8)


def new_step_id() -> str:
    return "as-" + secrets.token_hex(8)


def new_artifact_id() -> str:
    return "aa-" + secrets.token_hex(8)


# =====================================================================
# Die Firewall an jeder freitextigen Schreibstelle
# =====================================================================

def _refuse_credentials(*texts: str, where: str) -> None:
    """Verweigert, statt zu redigieren — der Zaun des Tresors am Buch.

    Ein Lauf traegt freien Text aus Spezialistenausgabe, Auftragstexten und
    Fehlermeldungen. Genau dort ist ein Token schon einmal in eine Datenbank
    gelangt, die niemand fuer geheimnisbehaftet hielt (Proactive-Store). Eine
    Ledger-Zeile ist eine Aussage: sie wird nicht halb geschrieben.
    """
    from solvio.secret_vault.firewall import any_credential, refuse_if_credential
    found = any_credential(*texts)
    if found:
        refuse_if_credential(found, where=where)


def _safe_text(text: str, limit: int, *, where: str) -> str:
    """Die eine Behandlung fuer jeden Freitext: verweigern, redigieren, deckeln.

    Reihenfolge ist Absicht. Die Verweigerung kommt ZUERST — sonst haette die
    Redaktion die Geheimnisgestalt entfernt und der Schreibvorgang waere still
    durchgelaufen, also genau das Bereinigen, das ADR-0029 ausschliesst. Die
    Redaktion des Starters laeuft danach als zweites Netz fuer Formen, die das
    eine Praedikat des Hauses nicht kennt.
    """
    value = str(text or "")
    _refuse_credentials(value, where=where)
    from solvio.specialists.launcher import redact
    return redact(value)[:limit]


def _safe_material_text(text: str, limit: int, *, where: str) -> str:
    """Der Zaun fuer Prosa-Spalten, die WERKZEUGMATERIAL tragen — Ergebnistext
    („Empfehlung des Spezialisten: <Empfehlung des Arbeiters>"), Schritt- und
    Ereigniszusammenfassungen. Die Aussage-Heuristik des Gedaechtnisses ueber
    den ganzen Text (Begriff + irgendein Doppelpunkt — den der Core hier selbst
    setzt) liess einen vollstaendig bestandenen Lauf am Wort „Schluessel" in der
    Arbeiter-Empfehlung FAILED enden (Review Runde 13, F13-1/K13-1). Wie beim
    Buch (ADR-0029, Containment-Suite): eine Schluesselform oder eine
    strukturelle Zugangsdaten-Zeile VERWEIGERT — nie bereinigt —, ein blosses
    Wort verweigert nichts mehr; danach das Redaktionsnetz.
    """
    from solvio.secret_vault.firewall import refuse_if_credential_material
    value = str(text or "")
    refuse_if_credential_material(value, where=where)
    from solvio.specialists.launcher import redact
    return redact(value)[:limit]


def _safe_json_record(text: str, limit: int, *, where: str) -> str:
    """Der Zaun fuer die JSON-Datensaetze eines Laufs — Urteil des Bewerters,
    Fortsetzungspunkt, Providergrenze, Anforderungsvertrag —, deren Texte
    Werkzeugmaterial tragen (Befunde, Quellen, woertliche Snapshot-Zeilen aus
    Dateiinhalten und Befehlsausgaben). Die Aussage-Heuristik des Gedaechtnisses
    (`Begriff + Doppelpunkt`) trifft in JSON jeden Begriff — „Schluessel" eines
    Dictionaries, „Token" eines Tokenizers — und liess den ganzen Datensatz
    verfallen: das Urteil still (`contextlib.suppress`, Anlauf y, Lauf 2) und mit
    ihm die Nacharbeit; den Fortsetzungspunkt als `result_not_retained` → FAILED
    `no_result` eines fertigen Auftrags (Anlauf z, Lauf 2 — ein Bericht ueber
    `csv.DictReader` und seine Schluessel; gemessen 19.09.2026). Daher
    (ADR-0029, Containment-Suite): eine Schluesselform oder eine strukturelle
    Zugangsdaten-Zeile in irgendeinem Textfeld VERWEIGERT den Datensatz — nie
    halb geschrieben, nie bereinigt —, ein blosses Wort verweigert nichts mehr.
    Die Bytes bleiben unveraendert (optimistische Schreiber vergleichen sie).
    Ungueltiges JSON faellt auf `_safe_text` zurueck.
    """
    from solvio.secret_vault.firewall import refuse_if_credential_material, refuse_if_key_shaped
    value = str(text or "")
    refuse_if_key_shaped(value, where=where)
    try:
        body = json.loads(value)
    except ValueError:
        return _safe_text(value, limit, where=where)

    def walk(node):
        if isinstance(node, str):
            refuse_if_credential_material(node, where=where)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                # Schluessel UND Wert als Zeile (`password: hunter2xyz`) — ein
                # arbeiterbestimmter Schluessel traegt sonst das Zugangsdatum am Zaun
                # vorbei (Review Runde 15, R15-3). Als NACKTER Wert, damit die Regel
                # fuer Material gilt (gemischt, R15-2): `"auth": "unknown"` ist eine
                # Core-Spalte, kein Zugangsdatum.
                if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    refuse_if_credential_material(f"{key}: {item}", where=where)
                walk(item)
    walk(body)
    from solvio.specialists.launcher import redact
    return redact(value)[:limit]

def _provider_route_reference(value: str) -> str:
    """Geschlossene Routenmetadaten erhalten; ungueltige Daten nie kuerzen."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PROVIDER_ROUTE_REF:
        raise ValueError("invalid_provider_route_reference")
    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid_provider_route_reference") from exc
    vocabulary = {
        "provider": {"codex", "claude-code", "hermes", "openai-api", "anthropic-api"},
        "billing_mode": {"subscription", "api", "metered_api", "unknown"},
        "phase": {"plan", "assessment", "specialist", "action_compose", "action_interpret"},
        "auth": {"", "subscription", "logged_out", "unknown", "api_key", "chatgpt", "oauth",
                 "claude.ai", "none"},
        "stage": {"requested", "observed"},
    }
    booleans = {"usage_reported", "dispatch_started"}
    native = {"runtime", "native_thread_id", "native_turn_id"}
    if (not isinstance(data, dict) or not (set(vocabulary) | booleans) <= set(data)
            or set(data) - (set(vocabulary) | booleans | native)):
        raise ValueError("invalid_provider_route_fields")
    for key, allowed in vocabulary.items():
        if not isinstance(data[key], str) or data[key] not in allowed:
            raise ValueError("invalid_provider_route_value")
    if any(type(data[key]) is not bool for key in booleans):
        raise ValueError("invalid_provider_route_value")
    if native & set(data):
        if data.get("runtime") != "hermes-codex-app-server" or data["provider"] != "codex":
            raise ValueError("invalid_native_runtime")
        for key in ("native_thread_id", "native_turn_id"):
            if key not in data:
                continue
            identifier = data[key]
            if (not isinstance(identifier, str) or not 0 < len(identifier) <= 128
                    or not identifier.isascii()
                    or not all(c.isalnum() or c in "._:-" for c in identifier)
                    or _safe_text(identifier, 128, where="agent_event.native_ref") != identifier):
                raise ValueError("invalid_native_reference")
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _native_progress_reference(value: str) -> str:
    data = json.loads(value)
    identifiers = {'invocation_id', 'operation_id', 'native_thread_id', 'native_turn_id', 'item_id'}
    if (not isinstance(data, dict) or set(data) != identifiers | {'runtime', 'seq', 'event', 'status'}
            or data['runtime'] not in ('hermes-codex-app-server', 'claude-bare-broker')
            or type(data['seq']) is not int or not 1 <= data['seq'] <= 24
            or (data['event'], data['status']) not in {
                ('started', ''), ('web_search', 'started'), ('web_search', 'completed'),
                ('browser_read', 'started'), ('browser_read', 'completed')}):
        raise ValueError('invalid_native_progress')
    claude = data['runtime'] == 'claude-bare-broker'
    if claude:
        # Claude emits only a start observation. Its existing turn identifier
        # is CLI-session UUID / cost claim (a handover may change the UUID).
        from uuid import UUID
        turn = data['native_turn_id']
        if (data['event'] != 'started' or data['seq'] != 1 or data['item_id'] != '' or not isinstance(turn, str)
                or turn.count('/') != 1):
            raise ValueError('invalid_native_progress')
        session_id, claim = turn.split('/')
        if str(UUID(session_id)) != session_id or claim != data['invocation_id']:
            raise ValueError('invalid_native_progress_identifier')
    for key in identifiers:
        val = data[key]
        separators = '._:-/' if claude and key == 'native_turn_id' else '._:-'
        if (not isinstance(val, str) or len(val) > 128 or (not val and key != 'item_id')
                or not val.isascii() or not all(c.isalnum() or c in separators for c in val)
                or _safe_text(val, 128, where='agent_event.native_progress') != val):
            raise ValueError('invalid_native_progress_identifier')
    return json.dumps(data, separators=(',', ':'), sort_keys=True)


def _require(vocabulary: frozenset[str], value: str, field_name: str) -> str:
    if value not in vocabulary:
        raise LedgerVocabularyError(field_name, str(value))
    return value


# =====================================================================
# Domaenentypen
# =====================================================================

@dataclass
class AgentTask:
    task_id: str
    objective: str
    scope: str
    target_repo: str = ""
    created_at: float = 0.0
    created_origin: str = ""
    created_principal: str = ""
    conversation_ref: str = ""
    predecessor_ref: str = ""
    #: Die gebundenen Anforderungen (JSON). Leer = keine — und dann gibt es
    #: keinen automatischen Abschluss.
    requirements: str = ""
    state: str = TASK_ACTIVE
    budget: dict = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class AgentRun:
    run_id: str
    task_id: str
    parent_run_id: str = ""
    attempt: int = 1
    state: str = CREATED
    plan_revision: int = 0
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    outcome: str = ""
    failure_category: str = ""
    result_summary: str = ""
    boundary: str = ""
    workspace_path: str = ""
    workspace_repo: str = ""
    workspace_branch: str = ""
    workspace_base: str = ""
    branch_ref: str = ""
    tokens_planner: int = 0
    specialist_seconds: float = 0.0
    specialist_count: int = 0
    #: Der Fortsetzungspunkt (siehe `agent_runtime/checkpoint.py`). Leer heisst
    #: „kein wiederherstellbarer Plan", nicht „nichts mehr zu tun".
    plan_checkpoint: str = ""
    #: Verbrauchte Planer-Aufrufe. Vor dem Modellaufruf gebunden, damit ein
    #: Absturz danach keinen geschenkten Aufruf hinterlaesst.
    planner_calls: int = 0
    #: Verbrauchte Bewertungsaufrufe. Dieselbe Bindung, eigener Topf.
    assessment_calls: int = 0
    #: Nur tatsaechlich gebuchte Provider-Wartezeit; started_at bleibt erhalten.
    provider_wait_seconds: float = 0.0
    provider_selection: str = ""
    #: Das letzte Bewertungsurteil (JSON). Leer = keines.
    completion_verdict: str = ""
    development_ref: str = ""
    updated_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def parked(self) -> bool:
        return self.state in PARKED_STATES


@dataclass
class AgentStep:
    step_id: str
    run_id: str
    seq: int
    kind: str
    state: str = "pending"
    attempt: int = 1
    specialist_profile: str = ""
    specialist_role: str = ""
    capability: str = ""
    call_id: str = ""
    approval_id: str = ""
    execution_id: str = ""
    #: N2: der einmalige Anspruch vor einem durch Aufgabenbefugnis getragenen
    #: Effekt. Er entsteht nur in task_authority.claim_step, nie update_step.
    dispatch_binding_digest: str = ""
    dispatch_claimed_at: float | None = None
    outcome_reason: str = ""
    child_pgid: int = 0
    child_started_at: float = 0.0
    child_executable: str = ""
    commit_ref: list = field(default_factory=list)
    artifact_refs: list = field(default_factory=list)
    summary: str = ""
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class AgentEvent:
    id: int
    at: float
    run_id: str
    step_id: str
    kind: str
    summary: str
    ref: str = ""


@dataclass
class AgentArtifact:
    artifact_id: str
    run_id: str
    kind: str
    path: str
    sha256: str
    bytes: int
    created_at: float


def _column(row, name: str, default: str = "") -> str:
    """Eine Spalte, die es geben SOLLTE. Fehlt sie, gilt die Vorgabe.

    Die Wanderung traegt sie beim Oeffnen nach; dieser Riegel steht daneben,
    weil ein `sqlite3.Row` bei einem unbekannten Namen wirft und ein Buch, das
    beim Lesen wirft, schlimmer ist als ein leeres Feld.
    """
    try:
        return str(row[name] or default)
    except (IndexError, KeyError):
        return default


def _to_task(row) -> AgentTask:
    try:
        budget = json.loads(row["budget"] or "{}")
    except ValueError:
        budget = {}
    return AgentTask(
        task_id=row["task_id"], objective=row["objective"], scope=row["scope"],
        target_repo=row["target_repo"], created_at=row["created_at"],
        created_origin=row["created_origin"], created_principal=row["created_principal"],
        conversation_ref=row["conversation_ref"],
        predecessor_ref=_column(row, "predecessor_ref"),
        requirements=_column(row, "requirements"), state=row["state"],
        budget=budget if isinstance(budget, dict) else {}, updated_at=row["updated_at"])


def _to_run(row) -> AgentRun:
    return AgentRun(
        run_id=row["run_id"], task_id=row["task_id"], parent_run_id=row["parent_run_id"],
        attempt=row["attempt"], state=row["state"], plan_revision=row["plan_revision"],
        created_at=row["created_at"], started_at=row["started_at"],
        finished_at=row["finished_at"], outcome=row["outcome"],
        failure_category=row["failure_category"], result_summary=row["result_summary"],
        boundary=row["boundary"], workspace_path=row["workspace_path"],
        workspace_repo=_column(row, "workspace_repo"),
        workspace_branch=_column(row, "workspace_branch"),
        workspace_base=_column(row, "workspace_base"),
        branch_ref=row["branch_ref"], tokens_planner=row["tokens_planner"],
        specialist_seconds=row["specialist_seconds"],
        specialist_count=row["specialist_count"],
        plan_checkpoint=_column(row, "plan_checkpoint"),
        planner_calls=int(_column(row, "planner_calls", "0") or 0),
        assessment_calls=int(_column(row, "assessment_calls", "0") or 0),
        provider_wait_seconds=float(_column(row, "provider_wait_seconds", "0") or 0),
        provider_selection=_column(row, "provider_selection"),
        completion_verdict=_column(row, "completion_verdict"),
        development_ref=_column(row, "development_ref"),
        updated_at=row["updated_at"])


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _to_step(row) -> AgentStep:
    return AgentStep(
        step_id=row["step_id"], run_id=row["run_id"], seq=row["seq"], kind=row["kind"],
        state=row["state"], attempt=row["attempt"],
        specialist_profile=row["specialist_profile"], specialist_role=row["specialist_role"],
        capability=row["capability"], call_id=row["call_id"],
        approval_id=row["approval_id"], execution_id=row["execution_id"],
        dispatch_binding_digest=row["dispatch_binding_digest"],
        dispatch_claimed_at=row["dispatch_claimed_at"],
        outcome_reason=row["outcome_reason"], child_pgid=row["child_pgid"],
        child_started_at=row["child_started_at"], child_executable=row["child_executable"],
        commit_ref=_json_list(row["commit_ref"]), artifact_refs=_json_list(row["artifact_refs"]),
        summary=row["summary"], started_at=row["started_at"], finished_at=row["finished_at"])


def _to_event(row) -> AgentEvent:
    return AgentEvent(id=row["id"], at=row["at"], run_id=row["run_id"],
                      step_id=row["step_id"], kind=row["kind"],
                      summary=row["summary"], ref=row["ref"])


def _to_artifact(row) -> AgentArtifact:
    return AgentArtifact(artifact_id=row["artifact_id"], run_id=row["run_id"],
                         kind=row["kind"], path=row["path"], sha256=row["sha256"],
                         bytes=row["bytes"], created_at=row["created_at"])


# =====================================================================
# Der Speicher
# =====================================================================

class AgentRunLedger:
    """Verbindungen je Operation, WAL, enge Rechte, geschlossene Vokabulare."""

    def __init__(self, path: str = "") -> None:
        self.path = resolve_path(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        # Der WAL-Modus legt zwei Beidateien an, und SQLite legt sie mit der
        # umask des Prozesses an — nicht mit den Rechten der Datenbank. Ohne die
        # enge umask waeren `-wal` und `-shm` 0644, waehrend die Datenbank 0600
        # ist; im `-wal` stehen die zuletzt geschriebenen Buchzeilen.
        previous = os.umask(0o077)
        try:
            with self._open() as connection:
                connection.executescript(SCHEMA)
                _add_missing_columns(connection)
        finally:
            os.umask(previous)
        # Auch ein bestehender Satz bekommt die engen Rechte: ein frueher zu
        # grosszuegig angelegter repariert sich damit selbst.
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(self.path + suffix, 0o600)

    # -- Verbindungen --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Beide gelten PRO VERBINDUNG, nicht pro Datenbank. Im Schema wirkte
        # `foreign_keys` genau einmal — auf der Verbindung, die das Schema
        # anlegte. Danach feuert keine Kaskade mehr.
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        # Es ist ein Journal: der Festschreibepunkt wird fsynct.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht.

        `with sqlite3.connect(...) as c:` sieht aus wie ein Schliessen und ist
        keines — es ist ein TRANSAKTIONS-Kontext. Ohne das `close()` im
        `finally` laufen unter einem Sekundentakt die Verbindungen auf, bis
        SQLite mit `unable to open database file` aufgibt.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def permissions_ok(self) -> bool:
        """Nicht reparieren, sondern melden: Rechte, die einmal offen standen,
        koennten bereits gelesen worden sein."""
        for suffix in ("", "-wal", "-shm"):
            try:
                mode = os.stat(self.path + suffix).st_mode & 0o777
            except OSError:
                continue
            if mode & 0o077:
                return False
        return True

    # -- Aufgaben ------------------------------------------------------

    def create_task(self, *, objective: str, scope: str, created_origin: str,
                    created_principal: str, target_repo: str = "",
                    conversation_ref: str = "", predecessor_ref: str = "",
                    budget: dict | None = None,
                    task_id: str = "") -> AgentTask:
        """Der Auftragstext steht GENAU EINMAL im Buch — hier."""
        _require(SCOPES, scope, "scope")
        now = time.time()
        task = AgentTask(
            task_id=task_id or new_task_id(),
            objective=_safe_text(objective, MAX_OBJECTIVE, where="agent_task.objective"),
            scope=scope, target_repo=str(target_repo or ""), created_at=now,
            created_origin=str(created_origin or ""),
            created_principal=str(created_principal or ""),
            conversation_ref=str(conversation_ref or ""),
            predecessor_ref=str(predecessor_ref or "")[:MAX_REF],
            state=TASK_ACTIVE,
            budget=dict(budget or {}), updated_at=now)
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_tasks (task_id, objective, scope, target_repo,"
                " created_at, created_origin, created_principal, conversation_ref,"
                " predecessor_ref, state, budget, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.objective, task.scope, task.target_repo,
                 task.created_at, task.created_origin, task.created_principal,
                 task.conversation_ref, task.predecessor_ref, task.state,
                 json.dumps(task.budget, ensure_ascii=False), task.updated_at))
        return task

    def get_task(self, task_id: str) -> AgentTask | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        return _to_task(row) if row else None

    def bind_requirements(self, task_id: str, payload: str) -> bool:
        """Die Anforderungen EINMAL binden — atomar, nicht lesen-dann-schreiben.

        Das `WHERE requirements=''` ist die ganze Zusicherung: zwei Wege, die
        gleichzeitig binden wollen, koennen sich nicht ueberholen, und ein
        zweiter Vorschlag aus einer Nachplanung faellt still ab, statt den
        ersten zu ueberschreiben. Rueckgabe `True` heisst „dieser Aufruf hat
        gebunden", `False` heisst „es gab schon einen Satz".

        Der Zaun des Buchs gilt auch hier: der Satz laeuft durch `_safe_text`.
        """
        if not payload:
            return False
        safe = _safe_json_record(payload, MAX_PLAN_CHECKPOINT,
                                 where="agent_task.requirements")
        with self._open() as connection:
            cursor = connection.execute(
                "UPDATE agent_tasks SET requirements=?, updated_at=?"
                " WHERE task_id=? AND requirements=''",
                (safe, time.time(), task_id))
            return bool(cursor.rowcount)

    def set_task_state(self, task_id: str, state: str) -> None:
        _require(TASK_STATES, state, "task_state")
        with self._open() as connection:
            connection.execute("UPDATE agent_tasks SET state=?, updated_at=? WHERE task_id=?",
                               (state, time.time(), task_id))

    # -- Laeufe --------------------------------------------------------

    def create_run(self, *, task_id: str, parent_run_id: str = "", attempt: int = 1,
                   run_id: str = "") -> AgentRun:
        now = time.time()
        run = AgentRun(run_id=run_id or new_run_id(), task_id=task_id,
                       parent_run_id=str(parent_run_id or ""), attempt=int(attempt),
                       state=CREATED, created_at=now, updated_at=now)
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, task_id, parent_run_id, attempt, state,"
                " plan_revision, created_at, outcome, failure_category, result_summary,"
                " boundary, workspace_path, branch_ref, tokens_planner,"
                " specialist_seconds, specialist_count, updated_at)"
                " VALUES (?,?,?,?,?,0,?,'','','','','','',0,0,0,?)",
                (run.run_id, run.task_id, run.parent_run_id, run.attempt, run.state,
                 run.created_at, run.updated_at))
        self.record_event(run.run_id, "state_changed", f"Lauf angelegt ({CREATED}).")
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return _to_run(row) if row else None

    def runs_for_task(self, task_id: str) -> list[AgentRun]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE task_id=? ORDER BY created_at",
                (task_id,)).fetchall()
        return [_to_run(row) for row in rows]

    def open_runs(self) -> list[AgentRun]:
        """Alle nicht-terminalen Laeufe — die Frage des Startabgleichs."""
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._open() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_runs WHERE state NOT IN ({placeholders})"
                " ORDER BY created_at", tuple(sorted(TERMINAL_STATES))).fetchall()
        return [_to_run(row) for row in rows]

    def active_runs(self) -> list[AgentRun]:
        """Laeufe, die einen Laufzeit-Slot belegen: offen UND nicht parkend."""
        return [run for run in self.open_runs() if not run.parked]

    def recent_runs(self, limit: int = 50, *, principal: str | None = None,
                    before: tuple[float, str] | None = None) -> list[AgentRun]:
        # Stable keyset pagination: newly created runs cannot shift older pages.
        # Ownership is applied in the same query, before the page boundary/limit.
        clauses, args = [], []
        if principal is not None:
            clauses.append("t.created_principal=?")
            args.append(principal)
        if before is not None:
            clauses.append("(r.created_at < ? OR (r.created_at = ? AND r.run_id < ?))")
            args.extend((before[0], before[0], before[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._open() as connection:
            rows = connection.execute("SELECT r.* FROM agent_runs r "
                "JOIN agent_tasks t ON t.task_id=r.task_id" + where +
                " ORDER BY r.created_at DESC, r.run_id DESC LIMIT ?",
                (*args, int(limit))).fetchall()
        return [_to_run(row) for row in rows]

    def task_overview_runs(self, *, principal: str) -> list[AgentRun]:
        """All current open tasks plus 25 recent closed tasks of this owner.

        History remains paginated separately. An old waiting task must never
        be displaced by newer completed work. No secondary task store.
        """
        terminal = tuple(sorted(TERMINAL_STATES))
        marks = ",".join("?" for _ in terminal)
        with self._open() as connection:
            rows = connection.execute(
                "WITH ranked AS (SELECT r.*, ROW_NUMBER() OVER ("
                "PARTITION BY r.task_id ORDER BY r.created_at DESC, r.attempt DESC, r.run_id DESC) AS position "
                "FROM agent_runs r JOIN agent_tasks t ON t.task_id=r.task_id "
                "WHERE t.created_principal=?) SELECT * FROM ranked WHERE position=1 "
                f"ORDER BY (state IN ({marks})), created_at DESC, run_id DESC",
                (principal, *terminal)).fetchall()
        runs, closed = [], 0
        for row in rows:
            if row["state"] in TERMINAL_STATES:
                closed += 1
                if closed > 25:
                    break
            runs.append(_to_run(row))
        return runs

    def transition(self, run_id: str, wanted: str, *, summary: str = "",
                   failure_category: str = "", result_summary: str = "",
                   outcome: str = "") -> AgentRun:
        """Die einzige Stelle, an der sich der Zustand eines Laufs aendert.

        Sie validiert den Ausgangszustand, schreibt einen Ereignissatz und
        setzt die Zeitstempel. Eine unerlaubte Kante wirft — auch dann, wenn
        der gewuenschte Zustand derselbe ist wie der aktuelle: ein
        Selbstuebergang steht in keiner Zeile der Tabelle.
        """
        _require(ALL_STATES, wanted, "state")
        if failure_category:
            _require(FAILURE_CATEGORIES, failure_category, "failure_category")
        now = time.time()
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown_run:{run_id}")
            current = row["state"]
            if wanted not in TRANSITIONS.get(current, frozenset()):
                raise LedgerTransitionError(run_id, current, wanted)
            started_at = row["started_at"]
            if started_at is None and wanted in (PLANNING, RUNNING):
                started_at = now
            finished_at = now if wanted in TERMINAL_STATES else row["finished_at"]
            resolved_outcome = outcome or row["outcome"]
            if wanted in TERMINAL_STATES and not resolved_outcome:
                resolved_outcome = {SUCCEEDED: "succeeded", FAILED: "failed",
                                    CANCELLED: "cancelled"}[wanted]
            safe_result = (_safe_material_text(result_summary, MAX_RESULT_SUMMARY,
                                               where="agent_run.result_summary")
                           if result_summary else row["result_summary"])
            connection.execute(
                "UPDATE agent_runs SET state=?, started_at=?, finished_at=?, outcome=?,"
                " failure_category=?, result_summary=?, updated_at=? WHERE run_id=?",
                (wanted, started_at, finished_at, resolved_outcome,
                 failure_category or row["failure_category"], safe_result, now, run_id))
        note = summary or f"{current} → {wanted}"
        self.record_event(run_id, "state_changed", note)
        return self.get_run(run_id)  # type: ignore[return-value]

    # -- Auftraege, die auf ihre erste Freigabe warten ------------------

    def remember_pending_start(self, *, request_id: str, capability: str,
                               arguments: dict, principal: str, origin: str,
                               commanded: bool, conversation_ref: str = "", delivery_ref: str = "") -> None:
        """Was noetig ist, um denselben Aufruf spaeter unveraendert zu wiederholen.

        **Keine Autoritaet, sondern ein Merkzettel.** Die Zeile bewirkt nichts;
        sie erlaubt nur, eine Anfragekennung erneut vorzulegen. Ob daraus eine
        Ausfuehrung wird, entscheidet unveraendert der Freigabeweg — Digest,
        Geraetebeweis, Einmaligkeit.

        Die HERKUNFT steht ausdruecklich dabei und wird nie neu erfunden: sie
        geht in den Freigabe-Digest ein. Eine per Raumstimme freigegebene
        Anfrage darf nicht als etwas Vertrauteres wiederholt werden.
        """
        if not request_id or not capability:
            raise LedgerError("pending start needs a request_id and a capability")
        if (bool(conversation_ref) != bool(delivery_ref) or conversation_ref and
                (not (capability == "gmail_send_draft" or capability == "background_create"
                      and arguments.get("aktion") in ("mail_antwort_pruefen", "tagesueberblick")) or origin != "trusted_interactive_app"
                 or not commanded or not re.fullmatch(r"c-[0-9a-f]{16}", conversation_ref)
                 or not re.fullmatch(r"cd-[0-9a-f]{16}", delivery_ref))):
            raise LedgerError("invalid_mail_delivery_binding")
        now = time.time()
        blob = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO pending_starts"
                " (request_id, capability, arguments, principal, origin,"
                "  commanded, state, created_at, updated_at, conversation_ref, delivery_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
                (request_id, capability, blob, principal or "", origin or "",
                 1 if commanded else 0, START_WAITING, now, now, conversation_ref, delivery_ref))
            row = conn.execute("SELECT * FROM pending_starts WHERE request_id=?", (request_id,)).fetchone()
            # Legacy registration remains insert-once, including conflicting
            # repeat callers. Only typed mail must refuse adoption/detachment.
            if (conversation_ref or row["conversation_ref"]) and (
                    row["capability"], row["arguments"], row["principal"], row["origin"],
                    bool(row["commanded"]), row["conversation_ref"], row["delivery_ref"]) != (
                    capability, blob, principal or "", origin or "", bool(commanded),
                    conversation_ref, delivery_ref):
                raise LedgerError("pending_start_binding_changed")
        log.info("agent_runtime.start_parked", capability=capability)

    def record_mail_outcome(self, request_id: str, *, summary: str, sent: bool | None) -> None:
        """Core result for a typed mail, in the existing continuation journal.

        The provider receipt must exist before sent=True. No model calls this.
        A terminal result cannot be replaced, including after restart.
        """
        _refuse_credentials(summary, where="pending_mail.outcome")
        if not summary or len(summary) > 2000 or sent is not None and type(sent) is not bool:
            raise LedgerError("invalid_mail_outcome")
        blob = json.dumps({"summary": summary, "sent": sent, "reported": False}, ensure_ascii=False)
        with self._open() as conn:
            conn.execute("UPDATE pending_starts SET mail_outcome=?, updated_at=? "
                         "WHERE request_id=? AND (capability='gmail_send_draft' OR "
                         "(capability='background_create' AND json_extract(arguments,'$.aktion') IN ('mail_antwort_pruefen','tagesueberblick'))) "
                         "AND conversation_ref<>'' AND mail_outcome='' AND state IN (?,?)",
                         (blob, time.time(), request_id, START_TAKEN, START_CLOSED))

    def unreported_mail_starts(self) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM pending_starts WHERE conversation_ref<>'' "
                "AND state<>? AND (mail_outcome='' OR json_extract(mail_outcome,'$.reported')=0) "
                "ORDER BY created_at LIMIT 100", (START_WAITING,)).fetchall()
        return [self._to_start(row) for row in rows]

    def mark_mail_reported(self, request_id: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE pending_starts SET mail_outcome=json_set(mail_outcome,'$.reported',1) "
                         "WHERE request_id=? AND mail_outcome<>'' AND conversation_ref<>''", (request_id,))

    @staticmethod
    def _to_start(row) -> dict:
        """Eine Zeile des Merkzettels — Argumente entpackt, Wahrheitswert echt.

        Eine Zeile, die aelter ist als die Spalte `run_id`, liest sich hier mit
        leerem `run_id`. Das ist die Wahrheit ueber sie, nicht ein Fehler.
        """
        entry = dict(row)
        try:
            entry["arguments"] = json.loads(entry["arguments"])
        except (ValueError, TypeError):
            entry["arguments"] = {}
        entry["commanded"] = bool(entry["commanded"])
        entry["run_id"] = str(entry.get("run_id") or "")
        return entry

    def waiting_starts(self) -> list[dict]:
        """Die Anfragen, die noch auf eine Entscheidung warten."""
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_starts WHERE state=? ORDER BY created_at",
                (START_WAITING,)).fetchall()
        return [self._to_start(row) for row in rows]

    def starts_for_capability(self, capability: str, *, limit: int = 20) -> list[dict]:
        """Die zuletzt geparkten Auftraege einer Faehigkeit — **nur lesend**.

        `waiting_starts` zeigt nur, was noch offen ist. Fuer eine Rueckfrage im
        NAECHSTEN Gespraech reicht das nicht: da ist der Auftrag laengst
        erledigt, und der Fragende kennt seine Kennung nicht mehr. Die Zeile
        bleibt beim Schliessen stehen (`close_pending_start` aendert nur den
        Zustand) — sie ist der einzige dauerhafte Ort, an dem Wortlaut und
        Kennung zusammenstehen.

        Es entsteht hier nichts und wird nichts veraendert.
        """
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_starts WHERE capability=?"
                " ORDER BY created_at DESC LIMIT ?",
                (capability, int(limit))).fetchall()
        return [self._to_start(row) for row in rows]

    def get_pending_start(self, request_id: str) -> dict | None:
        """Genau eine Zeile des Merkzettels — nur lesend."""
        if not request_id:
            return None
        with self._open() as conn:
            row = conn.execute("SELECT * FROM pending_starts WHERE request_id=?",
                               (request_id,)).fetchone()
        return self._to_start(row) if row else None

    def start_for_run(self, run_id: str) -> dict | None:
        """Die Startfreigabe, aus der ein Lauf entstanden ist — falls verzeichnet.

        `None` heisst nicht „ohne Freigabe entstanden", sondern „keine
        Verbindung verzeichnet": ein direkt freigegebener Start hat keine
        Zeile, und eine Zeile von vor dieser Spalte hat keinen Lauf.
        """
        if not run_id:
            return None
        with self._open() as conn:
            row = conn.execute("SELECT * FROM pending_starts WHERE run_id=?"
                               " ORDER BY created_at DESC LIMIT 1",
                               (run_id,)).fetchone()
        return self._to_start(row) if row else None

    def claim_pending_start(self, request_id: str) -> bool:
        """Exactly one poller may take a waiting request, before execution."""
        with self._open() as conn:
            cursor = conn.execute(
                "UPDATE pending_starts SET state=?, updated_at=?"
                " WHERE request_id=? AND state=?",
                (START_TAKEN, time.time(), request_id, START_WAITING))
            return cursor.rowcount == 1

    def close_pending_start(self, request_id: str, state: str) -> None:
        """Eine wartende Anfrage abschliessen — genommen oder erledigt."""
        if state not in START_STATES:
            raise LedgerError(f"unknown start state: {state}")
        with self._open() as conn:
            conn.execute(
                "UPDATE pending_starts SET state=?, updated_at=? WHERE request_id=?",
                (state, time.time(), request_id))

    def bind_pending_start_run(self, request_id: str, run_id: str) -> bool:
        """Den Lauf festhalten, der aus einer Startfreigabe entstanden ist.

        **Genau einmal, und nur aus gebundenen Daten.** Der Aufrufer ist der
        Poller, der die Freigabe eingeloest hat und die Kennung aus dem Umschlag
        derselben Ausfuehrung in der Hand haelt — nicht ein spaeterer Leser,
        der aus aehnlichem Wortlaut oder naher Uhrzeit auf eine Zuordnung
        schliesst. Eine Zeile, die schon einen Lauf traegt, bleibt unveraendert;
        `False` heisst dann „schon gebunden" oder „unbekannte Anfrage".
        """
        if not request_id or not run_id:
            return False
        with self._open() as conn:
            cursor = conn.execute(
                "UPDATE pending_starts SET run_id=?, updated_at=?"
                " WHERE request_id=? AND run_id=''",
                (str(run_id)[:MAX_REF], time.time(), request_id))
            bound = cursor.rowcount == 1
        if bound:
            log.info("agent_runtime.start_bound", run_id=run_id)
        return bound

    def bind_workspace(self, run_id: str, *, path: str, repo: str,
                       branch: str, base: str) -> None:
        """Den echten Klonpunkt einmalig und atomar mit seinem Pfad binden."""
        values = (path, repo, branch, base)
        for value, limit in zip(values, (2000, 2000, 200, 64)):
            if (not isinstance(value, str) or not value
                    or _safe_text(value, limit, where="agent_run.workspace_binding") != value):
                raise ValueError("invalid_workspace_binding")
        if not os.path.isabs(path) or not os.path.isabs(repo):
            raise ValueError("invalid_workspace_binding")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT r.*,t.scope FROM agent_runs r "
                "JOIN agent_tasks t ON t.task_id=r.task_id WHERE r.run_id=?", (run_id,)).fetchone()
            if row is None or row["scope"] != SCOPE_BUILD:
                raise ValueError("workspace_run_missing")
            previous = tuple(row[key] for key in
                ("workspace_path", "workspace_repo", "workspace_branch", "workspace_base"))
            if any(previous):
                if previous != values:
                    raise ValueError("workspace_binding_changed")
                return
            if row["state"] != CREATED:
                raise ValueError("workspace_binding_too_late")
            connection.execute("UPDATE agent_runs SET workspace_path=?,workspace_repo=?,"
                "workspace_branch=?,workspace_base=?,updated_at=? WHERE run_id=?",
                (*values, time.time(), run_id))

    def set_run_fields(self, run_id: str, **fields) -> None:
        """Die nicht-zustandsbehafteten Felder eines Laufs. Einzeln benannt —
        es gibt hier ausdruecklich kein `**kwargs` in die Datenbank hinein."""
        allowed = {
            "plan_revision": int, "workspace_path": str, "branch_ref": str,
            "tokens_planner": int, "specialist_seconds": float,
            "specialist_count": int, "parent_run_id": str,
            "planner_calls": int, "assessment_calls": int,
        }
        sets, values = [], []
        for name, caster in allowed.items():
            if name in fields:
                sets.append(f"{name}=?")
                values.append(caster(fields[name]))
        if "boundary" in fields:
            payload = fields["boundary"]
            text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) \
                else str(payload or "")
            sets.append("boundary=?")
            values.append(_safe_json_record(text, MAX_BOUNDARY_JSON, where="agent_run.boundary"))
        if "development_ref" in fields:
            sets.append("development_ref=?")
            values.append(str(fields["development_ref"] or "")[:MAX_REF])
        if "completion_verdict" in fields:
            sets.append("completion_verdict=?")
            values.append(_safe_json_record(str(fields["completion_verdict"] or ""),
                                            MAX_PLAN_CHECKPOINT,
                                            where="agent_run.completion_verdict")
                          if fields["completion_verdict"] else "")
        if "plan_checkpoint" in fields:
            # Der Zaun der JSON-Datensaetze (siehe _safe_json_record). Der Satz
            # ist bei der Kodierung bereits feldweise redigiert und gedeckelt;
            # das hier ist das Netz darunter, nicht statt dessen — und es
            # verwirft eine Schluesselform, nie mehr den Datensatz an einem Wort.
            sets.append("plan_checkpoint=?")
            values.append(_safe_json_record(str(fields["plan_checkpoint"] or ""),
                                            MAX_PLAN_CHECKPOINT,
                                            where="agent_run.plan_checkpoint")
                          if fields["plan_checkpoint"] else "")
        unknown = set(fields) - set(allowed) - {"boundary", "plan_checkpoint",
                                               "completion_verdict",
                                               "development_ref"}
        if unknown:
            raise LedgerVocabularyError("run_field", ",".join(sorted(unknown)))
        if not sets:
            return
        sets.append("updated_at=?")
        values.extend([time.time(), run_id])
        with self._open() as connection:
            connection.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE run_id=?",
                               tuple(values))

    def park_provider_boundary(self, run_id: str, boundary: dict, summary: str, *,
                               plan_checkpoint: str = "", step_id: str = "",
                               step_state: str = "", specialist_seconds: float = 0.0,
                               count_specialist: bool = True) -> bool:
        """Providergrenze und Parkzustand zusammen festschreiben, ohne neuen Lauf.

        Der Beginn der Wartezeit kommt ausschliesslich aus der Core-Uhr. Ein
        wiederholter Takt darf weder den Beginn verschieben noch doppelt melden.
        Schrittergebnis, Verbrauch und aktueller Checkpoint werden in derselben
        Transaktion gebunden: ein halber Park darf keinen Retry freigeben.
        """
        now = time.time()
        data = dict(boundary)
        wait = dict(data.get("provider_wait") or {})
        if wait.get("resume_state") not in (PLANNING, RUNNING, VERIFYING, WAITING_CAPABILITY):
            raise LedgerError("invalid_provider_resume_state")
        wait.update(status="waiting", since=now, boundary_id=secrets.token_hex(16))
        data["provider_wait"] = wait
        # JSON-Datensaetze und Werkzeugmaterial: eine Schluesselform verweigert, eine
        # Aussage-Zeile wird zum Marker — nie mehr die Providergrenze am Wort
        # „Schluessel" eines Befunds (Review Runde 13, K13-1: aus WAITING_USER
        # wurde FAILED capability_failed).
        encoded = _safe_json_record(json.dumps(data, ensure_ascii=False), MAX_BOUNDARY_JSON,
                                    where="agent_run.boundary")
        safe_summary = _safe_material_text(summary, MAX_RESULT_SUMMARY,
                                           where="agent_run.result_summary")
        checkpoint = _safe_json_record(plan_checkpoint, MAX_PLAN_CHECKPOINT,
                                       where="agent_run.plan_checkpoint") if plan_checkpoint else ""
        if step_id and step_state not in {"waiting", "unknown"}:
            raise LedgerError("invalid_provider_step_state")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM agent_runs WHERE run_id=?",
                                     (run_id,)).fetchone()
            if row is None or row["state"] == WAITING_USER:
                return False
            if WAITING_USER not in TRANSITIONS.get(row["state"], ()):
                raise LedgerTransitionError(run_id, row["state"], WAITING_USER)
            if step_id:
                changed = connection.execute(
                    "UPDATE agent_steps SET state=?, outcome_reason=?, summary=?, finished_at=? "
                    "WHERE step_id=? AND run_id=? AND state='running'",
                    (step_state, "provider_wait" if step_state == "waiting" else "provider_partial_work",
                     safe_summary[:MAX_STEP_SUMMARY], now, step_id, run_id)).rowcount
                if changed != 1:
                    raise LedgerError("provider_step_not_running")
            connection.execute(
                "UPDATE agent_runs SET state=?, boundary=?, result_summary=?, plan_checkpoint=?, "
                "specialist_count=specialist_count+?, specialist_seconds=specialist_seconds+?, updated_at=? "
                "WHERE run_id=?", (WAITING_USER, encoded, safe_summary, checkpoint,
                                    int(bool(step_id) and count_specialist), max(0.0, specialist_seconds), now, run_id))
        return True

    def provider_route_for_run(self, run_id: str) -> dict:
        """Die erste gebundene Planerroute; Spezialisten haben eigene Profile."""
        from solvio.agent_runtime import provider_switch as PS
        run = self.get_run(run_id)
        chosen = PS.selected(run, "plan") if run else ""
        if chosen:
            return {"provider": chosen, "billing_mode": "subscription", "phase": "plan"}
        with self._open() as connection:
            rows = connection.execute(
                "SELECT ref FROM agent_events WHERE run_id=? AND kind='provider_route' "
                "ORDER BY id", (run_id,)).fetchall()
        for row in rows:
            try:
                route = json.loads(row["ref"])
            except (ValueError, TypeError):
                continue
            if isinstance(route, dict) and route.get("phase") in ("plan", "assessment", "action_compose", "action_interpret"):
                return route
        return {}

    def resume_provider_boundary(self, run_id: str, *, provider: str,
                                 billing_mode: str) -> bool:
        """Owner-Resume derselben Route; Wartezeit genau einmal atomar buchen.

        Die Fortsetzungsphase bleibt bis zum naechsten Arbeitsschritt im
        bestehenden Grenzdatensatz. So verliert ein Absturz direkt nach der
        Wiederaufnahme nicht die Pflicht, erst zu planen oder zu pruefen.
        """
        now = time.time()
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id=?",
                                     (run_id,)).fetchone()
            if row is None or row["state"] != WAITING_USER:
                return False
            try:
                data = json.loads(row["boundary"] or "{}")
                wait = data["provider_wait"]
                if (wait.get("status") != "waiting" or not wait.get("resume_allowed")
                        or wait.get("provider") != provider
                        or wait.get("requested_billing_mode", wait.get("billing_mode")) != billing_mode):
                    return False
                wanted = wait["resume_state"]
                since = float(wait["since"])
            except (KeyError, TypeError, ValueError):
                return False
            if wanted not in (PLANNING, RUNNING, VERIFYING, WAITING_CAPABILITY) or since <= 0:
                return False
            seconds = max(0.0, now - since)
            wait.update(status="resuming", since=0)
            encoded = _safe_json_record(json.dumps(data, ensure_ascii=False), MAX_BOUNDARY_JSON,
                                        where="agent_run.boundary")
            connection.execute(
                "UPDATE agent_runs SET state=?, boundary=?, provider_wait_seconds="
                "provider_wait_seconds+?, updated_at=? WHERE run_id=?",
                (wanted, encoded, seconds, now, run_id))
        return True

    def switch_provider_boundary(self, run_id: str, *, provider: str,
                                 boundary_ref: str, principal: str) -> bool:
        """Bind the owner's exact selection and resume in one existing-ledger CAS."""
        from solvio.agent_runtime import provider_switch as PS, checkpoint as CP
        now = time.time()
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            run = _to_run(row) if row else None
            if run is None or PS.reference(run) != boundary_ref:
                return False
            boundary = PS.eligible(db, run, provider, principal, ledger=self)
            if boundary is None:
                return False
            wait = boundary["provider_wait"]
            choices = PS.selections(run.provider_selection)
            checkpoint = run.plan_checkpoint
            if wait["phase"] == "specialist":
                # Research routes and the native task workers are two
                # separate tables; the parked step says which one applies
                # (`eligible` has already verified it belongs to exactly one).
                step = db.execute("SELECT specialist_profile FROM agent_steps WHERE step_id=?",
                                  (boundary["schritt"],)).fetchone()
                table = PS.profile_table(step["specialist_profile"]) if step else None
                if table is None:
                    return False
                choices["worker" if table is PS.WORKER_PROFILES else "research"] = {
                    "provider": provider, "boundary_ref": boundary_ref}
                body = CP.decode(checkpoint)
                # The completed prefix is historical evidence of its actual
                # route. Only the parked step and untouched suffix change.
                for planned in body["schritte"][body["cursor"]:]:
                    if planned.get("profil") in table.values():
                        planned["profil"] = table[provider]
                checkpoint = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                if len(checkpoint.encode()) > MAX_PLAN_CHECKPOINT:
                    return False
                db.execute("UPDATE agent_steps SET specialist_profile=? WHERE step_id=?",
                           (table[provider], boundary["schritt"]))
            else:
                choices["planner"] = {"provider": provider, "boundary_ref": boundary_ref}
            seconds = max(0.0, now - float(wait["since"]))
            wait.update(status="resuming", since=0, provider=provider)
            encoded = json.dumps(boundary, ensure_ascii=False)
            if len(encoded.encode()) > MAX_BOUNDARY_JSON:
                raise ValueError("provider_boundary_too_large")
            db.execute("UPDATE agent_runs SET state=?,boundary=?,provider_selection=?,plan_checkpoint=?,"
                       "provider_wait_seconds=provider_wait_seconds+?,updated_at=? WHERE run_id=?",
                       (wait["resume_state"], encoded, json.dumps(choices, sort_keys=True),
                        checkpoint, seconds, now, run_id))
            db.execute("INSERT INTO agent_events(at,run_id,step_id,kind,summary,ref) VALUES(?,?,?,?,?,?)",
                (now, run_id, boundary.get("schritt", ""), "boundary_resumed",
                 "Der Nutzer setzt denselben Auftrag ausdrücklich mit " + provider + " fort.", boundary_ref))
            self._cap_events(db, run_id)
        return True

    # -- Schritte ------------------------------------------------------

    def create_step(self, *, run_id: str, seq: int, kind: str, attempt: int = 1,
                    specialist_profile: str = "", specialist_role: str = "",
                    capability: str = "", step_id: str = "") -> AgentStep:
        _require(STEP_KINDS, kind, "step_kind")
        step = AgentStep(step_id=step_id or new_step_id(), run_id=run_id, seq=int(seq),
                         kind=kind, state="pending", attempt=int(attempt),
                         specialist_profile=str(specialist_profile or ""),
                         specialist_role=str(specialist_role or ""),
                         capability=str(capability or ""))
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_steps (step_id, run_id, seq, kind, state, attempt,"
                " specialist_profile, specialist_role, capability)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (step.step_id, step.run_id, step.seq, step.kind, step.state, step.attempt,
                 step.specialist_profile, step.specialist_role, step.capability))
        return step

    def get_step(self, step_id: str) -> AgentStep | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_steps WHERE step_id=?", (step_id,)).fetchone()
        return _to_step(row) if row else None

    def steps_for_run(self, run_id: str) -> list[AgentStep]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_steps WHERE run_id=? ORDER BY seq, attempt",
                (run_id,)).fetchall()
        return [_to_step(row) for row in rows]

    def update_step(self, step_id: str, *, state: str = "", summary: str = "",
                    call_id: str = "", approval_id: str = "", execution_id: str = "",
                    outcome_reason: str = "", child_pgid: int | None = None,
                    child_started_at: float | None = None,
                    child_executable: str | None = None,
                    commit_ref: list | None = None,
                    artifact_refs: list | None = None,
                    started: bool = False, finished: bool = False) -> None:
        """Felder eines Schritts. Jedes einzeln benannt — es gibt keine Stelle,
        an der ein Adapter ein beliebiges Feld hineinreicht."""
        if state:
            _require(STEP_STATES, state, "step_state")
        sets, values = [], []
        if state:
            sets.append("state=?"); values.append(state)
        if summary:
            sets.append("summary=?")
            values.append(_safe_material_text(summary, MAX_STEP_SUMMARY, where="agent_step.summary"))
        for name, value in (("call_id", call_id), ("approval_id", approval_id),
                            ("execution_id", execution_id),
                            ("outcome_reason", outcome_reason)):
            if value:
                sets.append(f"{name}=?"); values.append(str(value)[:MAX_REF])
        if child_pgid is not None:
            sets.append("child_pgid=?"); values.append(int(child_pgid))
        if child_started_at is not None:
            sets.append("child_started_at=?"); values.append(float(child_started_at))
        if child_executable is not None:
            sets.append("child_executable=?"); values.append(str(child_executable)[:MAX_REF])
        if commit_ref is not None:
            sets.append("commit_ref=?")
            values.append(json.dumps([str(c)[:64] for c in commit_ref], ensure_ascii=False))
        if artifact_refs is not None:
            sets.append("artifact_refs=?")
            values.append(json.dumps([str(a)[:64] for a in artifact_refs], ensure_ascii=False))
        if started:
            sets.append("started_at=?"); values.append(time.time())
        if finished:
            sets.append("finished_at=?"); values.append(time.time())
        if not sets:
            return
        values.append(step_id)
        with self._open() as connection:
            connection.execute(f"UPDATE agent_steps SET {', '.join(sets)} WHERE step_id=?",
                               tuple(values))

    # -- Ereignisse ----------------------------------------------------

    def record_event(self, run_id: str, kind: str, summary: str, *,
                     step_id: str = "", ref: str = "") -> None:
        """Append-only. Genau EIN Verweis je Zeile, nie Material."""
        _require(EVENT_KINDS, kind, "event_kind")
        safe = _safe_material_text(summary, MAX_EVENT_SUMMARY, where="agent_event.summary")
        reference = (_provider_route_reference(ref) if kind == "provider_route" else
                     _native_progress_reference(ref) if kind == 'native_progress' else
                     str(ref or "")[:MAX_REF])
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_events (at, run_id, step_id, kind, summary, ref)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), run_id, str(step_id or ""), kind, safe, reference))
            self._cap_events(connection, run_id)

    def _cap_events(self, connection, run_id: str) -> None:
        """Aelteste zuerst. Ein Lauf, der Ereignisse produziert, darf das Buch
        nicht sprengen — und die Kappe steht als Zahl da, nicht als Hoffnung."""
        total = connection.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (run_id,)).fetchone()[0]
        if total <= MAX_EVENTS_PER_RUN:
            return
        connection.execute(
            "DELETE FROM agent_events WHERE id IN ("
            " SELECT id FROM agent_events WHERE run_id=? ORDER BY id LIMIT ?)",
            (run_id, total - MAX_EVENTS_PER_RUN))

    def events_for_run(self, run_id: str, limit: int = 200) -> list[AgentEvent]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, int(limit))).fetchall()
        return [_to_event(row) for row in reversed(rows)]

    def recent_events(self, limit: int = 100) -> list[AgentEvent]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_to_event(row) for row in rows]

    def events_after(self, run_id, after_id=0, limit=50):
        """One read snapshot; global event IDs need not be consecutive per run."""
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_event_cursor')
        with self._open() as connection:
            connection.execute('BEGIN DEFERRED')
            bounds = connection.execute('SELECT MIN(id) AS oldest,MAX(id) AS newest '
                'FROM agent_events WHERE run_id=?', (run_id,)).fetchone()
            rows = connection.execute('SELECT * FROM agent_events WHERE run_id=? AND id>? '
                'ORDER BY id ASC LIMIT ?', (run_id, after_id, limit + 1)).fetchall()
        events = [_to_event(row) for row in rows[:limit]]
        return {'events': events, 'next_cursor': events[-1].id if events else after_id,
                'has_more': len(rows) > limit, 'oldest_available': bounds['oldest'],
                'history_may_be_incomplete': bool(after_id and bounds['oldest'] and after_id < bounds['oldest'])}

    # -- Artefakte -----------------------------------------------------

    def add_artifact(self, *, run_id: str, kind: str, path: str, sha256: str,
                     size: int, artifact_id: str = "") -> AgentArtifact:
        _require(ARTIFACT_KINDS, kind, "artifact_kind")
        artifact = AgentArtifact(artifact_id=artifact_id or new_artifact_id(),
                                 run_id=run_id, kind=kind, path=str(path),
                                 sha256=str(sha256), bytes=int(size),
                                 created_at=time.time())
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_artifacts (artifact_id, run_id, kind, path, sha256,"
                " bytes, created_at) VALUES (?,?,?,?,?,?,?)",
                (artifact.artifact_id, artifact.run_id, artifact.kind, artifact.path,
                 artifact.sha256, artifact.bytes, artifact.created_at))
        return artifact

    def artifacts_for_run(self, run_id: str) -> list[AgentArtifact]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_artifacts WHERE run_id=? ORDER BY created_at",
                (run_id,)).fetchall()
        return [_to_artifact(row) for row in rows]

    # -- Aufbewahrung --------------------------------------------------

    def prune(self, *, now: float = 0.0) -> dict:
        """Terminal und abgelaufen wird beim Oeffnen gepruent.

        `agent_tasks` bleiben als Kopfzeilen stehen — die Frage „was habe ich
        dich damals gebeten?" ueberlebt laenger als das Material dazu. Die
        Kaskaden auf Schritte, Ereignisse und Artefakte haengen an
        `foreign_keys=ON` je Verbindung; ein Test loescht einen Lauf und
        verlangt, dass keine verwaiste Ereigniszeile uebrig bleibt.
        """
        current = now or time.time()
        cutoff = current - RETENTION_SECONDS
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._open() as connection:
            rows = connection.execute(
                f"SELECT run_id, workspace_path FROM agent_runs"
                f" WHERE state IN ({placeholders}) AND finished_at IS NOT NULL"
                f" AND finished_at < ?", tuple(sorted(TERMINAL_STATES)) + (cutoff,)
            ).fetchall()
            run_ids = [row["run_id"] for row in rows]
            for run_id in run_ids:
                connection.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))
        return {"runs_removed": len(run_ids), "run_ids": run_ids}

    def delete_run(self, run_id: str) -> None:
        """Nur fuer Aufraeumen und Tests. Die Kaskade ist die eigentliche Zusage."""
        with self._open() as connection:
            connection.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))

    # -- Zaehlungen fuer Probe und Chronik ------------------------------

    def counts(self, *, stuck_after: float = 0.0, now: float = 0.0) -> dict:
        """Was die Probe wissen will — aus denselben Zeilen, ohne zweite Wahrheit."""
        current = now or time.time()
        open_runs = self.open_runs()
        stuck = []
        if stuck_after > 0:
            with self._open() as connection:
                for run in open_runs:
                    row = connection.execute(
                        "SELECT MAX(at) AS last FROM agent_events WHERE run_id=?",
                        (run.run_id,)).fetchone()
                    last = (row["last"] if row and row["last"] else run.created_at)
                    if current - last > stuck_after:
                        stuck.append(run.run_id)
        return {
            "open": len(open_runs),
            "active": len([r for r in open_runs if not r.parked]),
            "waiting_approval": len([r for r in open_runs if r.state == WAITING_APPROVAL]),
            "waiting_user": len([r for r in open_runs if r.state == WAITING_USER]),
            "interrupted": len([r for r in open_runs if r.state == INTERRUPTED]),
            "stuck": stuck,
        }
