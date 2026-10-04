"""Das Entscheidungsbuch des Routers — Entscheidungen, nie Gedanken.

Dieselbe Haltung wie das Agent Run Ledger (ADR-0029), und aus demselben Grund:
ein Buch, in dem ein Gedankengang steht, ist irgendwann das Buch, aus dem
jemand einen Gedankengang liest.

**WAS HIER STEHT — UND WAS NICHT**

Es steht drin: welche Route gewaehlt wurde, welche vorgeschlagen war, auf
welcher Stufe, mit welchem Ereignis, wie sicher, wie lange, was dabei
herauskam. Es steht NICHT drin: der Aeusserungstext, die Eingabe an den
Einschaetzer, seine Rohantwort, irgendeine Begruendung in freier Sprache. Der
Aeusserungstext wohnt im Gespraechsspeicher und wird per `turn_ref`
referenziert — eine zweite Kopie der Sprache des Nutzers waere genau die
zweite Wahrheit, die dieses Projekt an anderer Stelle schon kuriert hat.

Der Schleifenzaun braucht keinen Text: er braucht einen **Abdruck**
(`objective_digest`), und ein Abdruck ist keine Aussage ueber Inhalt.

**SQLITE-DISZIPLIN**

Verbindung je Operation mit `close()` im `finally` (ein `with connect(...)` ist
ein TRANSAKTIONS-Kontext und kein Schliessen), `foreign_keys`/`busy_timeout`/
`synchronous` PRO VERBINDUNG, WAL im Schemakopf, enge Rechte auch fuer `-wal`
und `-shm` — dort stehen die zuletzt geschriebenen Zeilen.

**ZEIT IST INJIZIERBAR.** `at`/`now` kommen als Argument herein, damit ein Test
die Uhr besitzt und die Aufbewahrungsgrenze ueberhaupt pruefbar ist.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import sqlite3
import time
from typing import Any

from solvio.cognition.taxonomy import FAILURE_KINDS
from solvio.cognition.types import (ESCALATION_EVENTS, OBSERVED_KINDS, OUTCOMES,
                                    ROUTES, RoutingDecision)
from solvio.logging_setup import get_logger

log = get_logger("cognition")

DEFAULT_PATH = "~/.solvio/cognition.sqlite3"

#: Testschalter. Ein Test schreibt nie in das produktive Buch — und dieser Core
#: laeuft produktiv aus dem Arbeitsbaum, also ist das hier kein Formalismus.
PATH_ENV = "SOLVIO_COGNITION_DB"


def state_dir() -> str:
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def resolve_path(path: str = "") -> str:
    """Ausdrueckliches Argument schlaegt Umgebung schlaegt Vorgabe."""
    chosen = path or os.environ.get(PATH_ENV, "")
    if not chosen:
        chosen = os.path.join(state_dir(), "cognition.sqlite3")
    return os.path.abspath(os.path.expanduser(chosen))


# -- Laengendeckel ------------------------------------------------------------
#
# Sie sind der Grund, warum kein Transkript hineinpasst. Ein Deckel, den man
# erhoehen muesste, um eine Aeusserung abzulegen, ist eine sichtbare
# Entscheidung — genau das ist beabsichtigt.
MAX_REF = 200
MAX_DIGEST = 40

#: Aufbewahrung: wie bei Konversationen.
RETENTION_SECONDS = 90 * 24 * 3600

#: Kappe je Konversation. Ein Gespraech, das den Router hundertmal ruft, darf
#: das Buch nicht sprengen — und die Kappe steht als Zahl da, nicht als
#: Hoffnung.
MAX_DECISIONS_PER_CONVERSATION = 500


class LedgerError(RuntimeError):
    """Basis: etwas am Buch stimmt nicht."""


class LedgerVocabularyError(LedgerError):
    """Ein Wort ausserhalb eines geschlossenen Vokabulars.

    Die Meldung traegt das Wort und NIE den Text, in dem es stand.
    """

    def __init__(self, field_name: str, value: str) -> None:
        super().__init__(f"unknown_{field_name}:{value}")
        self.field_name = field_name
        self.value = value


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id        TEXT PRIMARY KEY,
    at                 REAL NOT NULL,
    conversation_ref   TEXT NOT NULL DEFAULT '',
    turn_ref           TEXT NOT NULL DEFAULT '',
    origin             TEXT NOT NULL DEFAULT '',
    objective_digest   TEXT NOT NULL DEFAULT '',
    route_proposed     TEXT NOT NULL DEFAULT '',
    route_final        TEXT NOT NULL DEFAULT '',
    consult_role       TEXT NOT NULL DEFAULT '',
    preference         TEXT NOT NULL DEFAULT '',
    tier               TEXT NOT NULL DEFAULT '',
    escalation_event   TEXT NOT NULL DEFAULT '',
    confidence         REAL NOT NULL DEFAULT 0.0,
    difficulty         TEXT NOT NULL DEFAULT '',
    continuity_ref     TEXT NOT NULL DEFAULT '',
    produced_ref       TEXT NOT NULL DEFAULT '',
    outcome            TEXT NOT NULL DEFAULT '',
    failure_kind       TEXT NOT NULL DEFAULT '',
    observed_tool      TEXT NOT NULL DEFAULT '',
    observed_kind      TEXT NOT NULL DEFAULT '',
    observed_approval  INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    tokens_assessment  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_decisions_conversation
    ON routing_decisions(conversation_ref, at);
CREATE INDEX IF NOT EXISTS idx_decisions_digest
    ON routing_decisions(conversation_ref, objective_digest, at);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON routing_decisions(at);
"""

#: Spalten, die eine BESTEHENDE Ablage nachtraegt. Nur ADDITIV — es wird nie
#: eine Spalte entfernt und nie eine umgeschrieben. `CREATE TABLE IF NOT
#: EXISTS` ruehrt eine vorhandene Tabelle nicht an; ohne diese Wanderung kaeme
#: eine neue Spalte bei niemandem an, der die Datei schon hat.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "routing_decisions": {
        "observed_tool": "TEXT NOT NULL DEFAULT ''",
        "observed_kind": "TEXT NOT NULL DEFAULT ''",
        "observed_approval": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _add_missing_columns(connection: sqlite3.Connection) -> None:
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
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            log.info("cognition.column_added", table=table, column=name)


# =====================================================================
# Kennungen und Abdruck — vom Core gepraegt, nie vom Modell
# =====================================================================

def new_decision_id() -> str:
    return "rd-" + secrets.token_hex(8)


#: Dieselbe Normalisierung wie der Versuchsabdruck der Agentenlaufzeit: alles
#: klein, alles Nicht-Alphanumerische zu einem Leerzeichen, Mehrfach-Leerraum
#: zusammengezogen. Sie faengt woertliche Wiederholungen mit anderer
#: Zeichensetzung — und ausdruecklich nicht mehr.
_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalise(text: str) -> str:
    return _NON_WORD.sub(" ", str(text or "").lower()).strip()


def objective_digest(text: str) -> str:
    """Der Abdruck einer Aeusserung — nie die Aeusserung.

    Was er kann: dieselbe Bitte, gleich noch einmal gesagt, wiedererkennen.
    Was er nicht kann: umformulierte Gleicharbeit erkennen (die kostet eine
    frische Einschaetzung), und gleichlautende Bitten ueber VERSCHIEDENE
    Bezugsobjekte auseinanderhalten — ein benanntes Produktresiduum des
    bewusst stichwortfreien Entwurfs (DEBT-0147).
    """
    normalised = normalise(text)
    if not normalised:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:20]


def _require(vocabulary: frozenset[str], value: str, field_name: str) -> str:
    if value not in vocabulary:
        raise LedgerVocabularyError(field_name, str(value))
    return value


def _ref(value: str) -> str:
    """Eine Kennung, gekappt. Verweise sind kurz oder falsch."""
    return str(value or "")[:MAX_REF]


class CognitionLedger:
    """Verbindungen je Operation, WAL, enge Rechte, geschlossene Vokabulare."""

    def __init__(self, path: str = "") -> None:
        self.path = resolve_path(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        # Der WAL-Modus legt zwei Beidateien an, und SQLite legt sie mit der
        # umask des Prozesses an — nicht mit den Rechten der Datenbank. Ohne
        # die enge umask waeren `-wal` und `-shm` 0644, waehrend die Datenbank
        # 0600 ist; im `-wal` stehen die zuletzt geschriebenen Buchzeilen.
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
        # Beide gelten PRO VERBINDUNG, nicht pro Datenbank.
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

    # -- Schreiben -----------------------------------------------------

    def record(self, decision: RoutingDecision) -> str:
        """Eine Entscheidung, aufgezeichnet. Die Spalten sind aufgezaehlt.

        Kein `**kwargs`: eine Buchungsschnittstelle, in die ein Aufrufer ein
        beliebiges Feld schieben kann, ist die Stelle, an der irgendwann ein
        Geheimnis in einem Buch landet.
        """
        _require(ROUTES | {""}, decision.route_proposed, "route")
        _require(ROUTES | {""}, decision.route_final, "route")
        _require(OUTCOMES, decision.outcome, "outcome")
        _require(FAILURE_KINDS, decision.failure_kind, "failure_kind")
        _require(ESCALATION_EVENTS, decision.escalation_event, "escalation_event")
        _require(OBSERVED_KINDS, decision.observed_kind, "observed_kind")
        with self._open() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO routing_decisions ("
                " decision_id, at, conversation_ref, turn_ref, origin,"
                " objective_digest, route_proposed, route_final, consult_role,"
                " preference, tier, escalation_event, confidence, difficulty,"
                " continuity_ref, produced_ref, outcome, failure_kind,"
                " observed_tool, observed_kind, observed_approval,"
                " duration_ms, tokens_assessment"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision.decision_id, float(decision.at),
                 _ref(decision.conversation_ref), _ref(decision.turn_ref),
                 _ref(decision.origin),
                 str(decision.objective_digest or "")[:MAX_DIGEST],
                 decision.route_proposed, decision.route_final,
                 _ref(decision.consult_role), _ref(decision.preference),
                 _ref(decision.tier), decision.escalation_event,
                 float(decision.confidence), _ref(decision.difficulty),
                 _ref(decision.continuity_ref), _ref(decision.produced_ref),
                 decision.outcome, decision.failure_kind,
                 _ref(decision.observed_tool), decision.observed_kind,
                 int(bool(decision.observed_approval)),
                 int(decision.duration_ms), int(decision.tokens_assessment)))
            self._cap_conversation(connection, decision.conversation_ref)
        log.info("cognition.decision", decision_id=decision.decision_id,
                 route=decision.route_final, tier=decision.tier,
                 escalation=decision.escalation_event,
                 outcome=decision.outcome, ms=int(decision.duration_ms))
        return decision.decision_id

    def _cap_conversation(self, connection: sqlite3.Connection,
                          conversation_ref: str) -> None:
        """Aelteste zuerst."""
        reference = _ref(conversation_ref)
        if not reference:
            return
        total = connection.execute(
            "SELECT COUNT(*) FROM routing_decisions WHERE conversation_ref=?",
            (reference,)).fetchone()[0]
        if total <= MAX_DECISIONS_PER_CONVERSATION:
            return
        connection.execute(
            "DELETE FROM routing_decisions WHERE decision_id IN ("
            " SELECT decision_id FROM routing_decisions WHERE conversation_ref=?"
            " ORDER BY at LIMIT ?)",
            (reference, total - MAX_DECISIONS_PER_CONVERSATION))

    def set_produced_ref(self, decision_id: str, produced_ref: str) -> None:
        """Der erzeugte Verweis, nachgetragen. Nur dieses eine Feld."""
        with self._open() as connection:
            connection.execute(
                "UPDATE routing_decisions SET produced_ref=? WHERE decision_id=?",
                (_ref(produced_ref), str(decision_id or "")))

    # -- Lesen ---------------------------------------------------------

    def recent(self, conversation_ref: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Die juengsten Entscheidungen DIESER Konversation. Neueste zuerst.

        Konversationsgebunden, und das ist keine Bequemlichkeit: ein
        Arbeitsregister, das fremde Konversationen enthielte, waere der Weg,
        auf dem eine Kennung von aussen in eine Fortsetzung geriete.
        """
        reference = _ref(conversation_ref)
        if not reference:
            return []
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM routing_decisions WHERE conversation_ref=?"
                " ORDER BY at DESC LIMIT ?",
                (reference, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def with_digest(self, conversation_ref: str, digest: str, *,
                    since: float = 0.0) -> list[dict[str, Any]]:
        """Entscheidungen mit demselben Abdruck — in dieser Konversation."""
        reference = _ref(conversation_ref)
        if not reference or not digest:
            return []
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM routing_decisions WHERE conversation_ref=?"
                " AND objective_digest=? AND at>=? ORDER BY at DESC",
                (reference, str(digest)[:MAX_DIGEST], float(since))).fetchall()
        return [dict(row) for row in rows]

    def shadow_count_since(self, since: float) -> int:
        """Wie viele Schatten-Einschaetzungen seit diesem Zeitpunkt."""
        with self._open() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM routing_decisions"
                " WHERE outcome='shadow' AND at>=?", (float(since),)).fetchone()
        return int(row[0]) if row else 0

    def counts(self, *, now: float = 0.0, window: float = 3600.0) -> dict[str, int]:
        """Was die Sonde wissen will — aus denselben Zeilen, ohne zweite
        Wahrheit."""
        current = now or time.time()
        since = current - window
        with self._open() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
            recent = connection.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE at>=?",
                (since,)).fetchone()[0]
            failed = connection.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE at>=?"
                " AND outcome='failed'", (since,)).fetchone()[0]
            escalated = connection.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE at>=?"
                " AND tier='large'", (since,)).fetchone()[0]
        return {"total": int(total), "recent": int(recent),
                "failed": int(failed), "escalated": int(escalated)}

    # -- Aufbewahrung --------------------------------------------------

    def prune(self, *, now: float = 0.0) -> int:
        """Was aelter ist als die Aufbewahrung, geht. Traege, beim Oeffnen."""
        cutoff = (now or time.time()) - RETENTION_SECONDS
        with self._open() as connection:
            cursor = connection.execute(
                "DELETE FROM routing_decisions WHERE at < ?", (cutoff,))
            removed = int(cursor.rowcount or 0)
        if removed:
            log.info("cognition.pruned", removed=removed)
        return removed
