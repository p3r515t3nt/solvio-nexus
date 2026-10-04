"""Das Buch des Brokers — was durchging, was abgelehnt wurde, was es kostete.

Eine Datei, eine anhaengende Tabelle. Sie beantwortet drei Fragen, die sonst
niemand beantworten kann: lag **jeder** Anbieteraufruf des Kaefigs in einem
offenen Lease, was hat ein Tag wirklich gekostet, und wo wurde etwas abgewiesen.

**Was hier niemals hineingeht:** Aufforderungsinhalt, Antwortinhalt, ein
Bearer, ein Broker-Token, der Anbieterschluessel, ein `Authorization`-Kopf oder
der rohe Fehlertext des Anbieters. Die Spalten sind **aufgezaehlt** und nicht
`**kwargs` — eine Buchungsschnittstelle, in die ein Aufrufer ein beliebiges Feld
schieben kann, ist die Stelle, an der ein Geheimnis in ein Buch geraet.

**Eine Zeile mit `tokens_source='estimated'` ist kein Fehler**, sondern der
Normalfall fuer einen abgebrochenen Strom: `usage` erreicht den Broker erst im
letzten Stromereignis und bei abgeklemmter Verbindung nie. Sie darf nur nie
stillschweigend zu null werden — genau das waere die Luecke, durch die eine
Tokenkappe auf null zu setzen waere.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("broker")

DEFAULT_PATH = "~/.solvio/broker.sqlite3"

#: Testschalter. Ein Test schreibt nie in das produktive Buch.
PATH_ENV = "SOLVIO_BROKER_DB"

#: Ab wann beschnitten wird. Das Buch ist Betriebsbeleg, kein Archiv.
MAX_ROWS = 20_000

#: Die geschlossene Menge der Ablehnungsgruende. Ein Wert ausserhalb ist ein
#: Programmierfehler und wird beim Schreiben zurueckgewiesen — sonst wandert
#: irgendwann ein Freitext mit Inhalt in diese Spalte.
DENIED_REASONS = frozenset({
    "bad_token", "stale_generation", "lease_absent", "rate_capped",
    "token_capped", "path_not_allowed", "model_not_allowed", "body_too_large",
    # Aufgabengrenzen. Sie sind KEINE Tageskappe und duerfen nie als eine
    # gelesen werden: `token_capped` erholt sich um UTC-Mitternacht, diese drei
    # nie — das Budget dieses einen Auftrags fuellt sich nicht nach.
    "lease_budget_exhausted", "lease_request_capped",
    # Ein byte-gleicher Rumpf, der in diesem Lease schon EINMAL geliefert
    # wurde. Rueckfallnetz gegen einen blinden Applikations-Retry.
    "duplicate_request",
    # Die drei der Anthropic-Flaeche (V0.6). Sie stehen hier, damit eine
    # Absage buchbar ist — eine Absage, die das Buch nicht kennt, waere eine
    # Absage, die niemand nachzaehlen kann.
    #
    # `principal_not_allowed`: ein gueltiges Token, aber nicht fuer DIESE
    #   Flaeche. Deep, Bots und der Lead haben dort nichts zu suchen.
    # `no_credential`: es liegt keine Anthropic-Anmeldung im Tresor. Der
    #   Autopilot liest das und meldet den Schreiber UNAVAILABLE.
    # `credential_denied` / `credential_malformed` / `vault_unavailable`: der
    #   Tresor sagte nein, der Wert war unlesbar, oder er war nicht zu haben.
    "principal_not_allowed", "no_credential", "credential_denied",
    "credential_malformed", "vault_unavailable",
    # Das Broker-Tor fuer anbieterseitige Werkzeuge. `type` in `tools[]` ist
    # nicht `function` UND nicht in `Caps.allowed_provider_tools` dieses
    # Auftraggebers — Voreinstellung fuer jeden Auftraggeber ist die LEERE
    # Menge. Ein Kaefig, der `code_interpreter` nennt, bekommt genau diesen
    # Grund und `403`, nie eine stille Weiterleitung.
    "provider_tool_not_allowed",
})

#: `answered_locally` steht nicht im ersten Entwurf, und es fehlte dort. Eine
#: lokal beantwortete Modellliste als `forwarded` zu buchen waere eine falsche
#: Aussage: weitergeleitet wurde nichts, der Anbieter hat die Anfrage nie
#: gesehen. Genau diese Spalte soll aber die Frage beantworten, ob JEDER
#: Anbieteraufruf in einem offenen Lease lag — eine Zeile, die eine Weiterleitung
#: behauptet, die nie stattfand, verdirbt die Antwort.
#: `client_aborted` steht nicht im ersten Entwurf. Bricht der Klient einen
#: Strom ab, NACHDEM der Anbieter mit einem Statuscode unter 400 geantwortet
#: hat, ist das kein Fehler des Anbieters — der Abbruch kommt vom Kaefig, der
#: die Verbindung zumacht, waehrend das Schreiben zum Klienten noch laeuft.
#: Als `upstream_error` gebucht waere das eine falsche Aussage: der Anbieter
#: hat geantwortet, der Strom wurde nur nicht zu Ende gelesen.
OUTCOMES = frozenset({
    "forwarded", "denied", "upstream_error", "answered_locally",
    "client_aborted",
})

#: Woher die gebuchte Zahl kommt. Geschlossen, mit Grund je Eintrag:
#: `reported` = der Anbieter hat `usage` gemeldet; `measured_bytes` = er hat
#: nicht, weil der Klient abbrach, und gebucht ist, was nachweislich floss;
#: `estimated` = weder noch, die Vorbelastung bleibt stehen.
TOKEN_SOURCES = frozenset({"estimated", "reported", "measured_bytes"})

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS broker_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             REAL    NOT NULL,
    principal      TEXT    NOT NULL,
    generation     INTEGER NOT NULL,
    lease_id       TEXT    NOT NULL DEFAULT '',
    task_ref       TEXT    NOT NULL DEFAULT '',
    method         TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    model          TEXT    NOT NULL DEFAULT '',
    status_code    INTEGER NOT NULL DEFAULT 0,
    request_bytes  INTEGER NOT NULL DEFAULT 0,
    response_bytes INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    tokens_source  TEXT    NOT NULL DEFAULT '',
    outcome        TEXT    NOT NULL,
    denied_reason  TEXT    NOT NULL DEFAULT '',
    request_sha256 TEXT    NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS broker_ledger_at ON broker_ledger(at);
CREATE INDEX IF NOT EXISTS broker_ledger_principal ON broker_ledger(principal, at);
"""


def resolve_path(path: str = "") -> str:
    """Wohin das Buch gehoert. Umgebung schlaegt Vorgabe, damit Tests nie
    in das produktive Buch schreiben."""
    chosen = path or os.environ.get(PATH_ENV, "") or DEFAULT_PATH
    return os.path.abspath(os.path.expanduser(chosen))


@dataclass(frozen=True)
class Entry:
    """Eine Zeile. Aufgezaehlt, damit nichts Unerwartetes mitreist."""

    principal: str
    generation: int
    method: str
    path: str
    outcome: str
    lease_id: str = ""
    task_ref: str = ""
    model: str = ""
    status_code: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    tokens_source: str = ""
    denied_reason: str = ""
    #: SHA-256 ueber den ohnehin voll gepufferten Anfragerumpf. **Ein Hash,
    #: nie der Rumpf.** Er macht Byte-Identitaet beweisbar statt nur
    #: Byte-GLEICHSTAND (`request_bytes`) und traegt den Duplikat-Schutz. Aus
    #: ihm laesst sich kein Aufforderungsinhalt zurueckgewinnen.
    request_sha256: str = ""


class BrokerLedger:
    """Anhaengendes Buch. Kein UPDATE ausser dem Nachtragen echter `usage`."""

    def __init__(self, path: str = "") -> None:
        self.path = resolve_path(path)
        self._db: sqlite3.Connection | None = None

    def open(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        # Der WAL-Modus legt zwei Beidateien an, und SQLite legt sie mit der
        # umask des Prozesses an — nicht mit den Rechten der Datenbank. Ohne die
        # enge umask hier waeren `-wal` und `-shm` `0644`, waehrend die
        # Datenbank `0600` ist; im `-wal` stehen die zuletzt geschriebenen
        # Buchzeilen. Die uebrigen Speicher dieses Projekts machen es genauso.
        previous = os.umask(0o077)
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(SCHEMA)
            self._migrate(self._db)
            self._db.commit()
        finally:
            os.umask(previous)
        # Auch ein bestehender Satz bekommt die engen Rechte: ein frueher zu
        # grosszuegig angelegter repariert sich damit selbst.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def record(self, entry: Entry, *, at: float) -> int:
        """Schreibt eine Zeile und liefert ihre Kennung zurueck.

        `at` wird hereingereicht statt hier gelesen, damit ein Test die Uhr
        besitzt und die UTC-Tagesgrenze pruefen kann.
        """
        if entry.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {entry.outcome!r}")
        if entry.denied_reason and entry.denied_reason not in DENIED_REASONS:
            raise ValueError(f"unknown denied_reason: {entry.denied_reason!r}")
        if entry.tokens_source and entry.tokens_source not in TOKEN_SOURCES:
            raise ValueError(f"unknown tokens_source: {entry.tokens_source!r}")
        db = self._require()
        cursor = db.execute(
            "INSERT INTO broker_ledger ("
            " at, principal, generation, lease_id, task_ref, method, path, model,"
            " status_code, request_bytes, response_bytes, input_tokens,"
            " output_tokens, duration_ms, tokens_source, outcome, denied_reason,"
            " request_sha256)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (float(at), entry.principal, int(entry.generation), entry.lease_id,
             entry.task_ref, entry.method, entry.path, entry.model,
             int(entry.status_code), int(entry.request_bytes),
             int(entry.response_bytes), int(entry.input_tokens),
             int(entry.output_tokens), int(entry.duration_ms),
             entry.tokens_source, entry.outcome, entry.denied_reason,
             entry.request_sha256))
        db.commit()
        self._trim()
        return int(cursor.lastrowid or 0)

    def settle(self, row_id: int, *, input_tokens: int, output_tokens: int,
               response_bytes: int, status_code: int, duration_ms: int,
               outcome: str, tokens_source: str) -> None:
        """Traegt den Ausgang einer bereits gebuchten Weiterleitung nach.

        Das ist der einzige UPDATE auf diesem Buch, und er beruehrt genau die
        Spalten, die beim Anlegen der Zeile noch nicht bekannt sein KONNTEN —
        `usage` kommt erst im letzten Stromereignis. Der Auftraggeber, der Pfad
        und das Modell einer Zeile aendern sich nie.
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome!r}")
        if tokens_source not in TOKEN_SOURCES:
            raise ValueError(f"unknown tokens_source: {tokens_source!r}")
        db = self._require()
        db.execute(
            "UPDATE broker_ledger SET input_tokens=?, output_tokens=?,"
            " response_bytes=?, status_code=?, duration_ms=?, outcome=?,"
            " tokens_source=? WHERE id=?",
            (int(input_tokens), int(output_tokens), int(response_bytes),
             int(status_code), int(duration_ms), outcome, tokens_source,
             int(row_id)))
        db.commit()

    def rows(self, *, limit: int = 200) -> list[dict[str, Any]]:
        db = self._require()
        cursor = db.execute(
            "SELECT * FROM broker_ledger ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(row) for row in cursor.fetchall()]

    def usage_since(self, *, principal: str, since: float) -> dict[str, int]:
        """Was ein Auftraggeber seit `since` verbraucht hat.

        Die Quelle der Tokenkappe beim Neustart: die Registratur ist
        fluechtig, das Buch nicht. Ohne diese Zeile waere eine Tageskappe mit
        einem Core-Neustart zurueckgesetzt.

        `client_aborted` zaehlt hier mit, obwohl es kein Anbieterfehler ist:
        der Anbieter hat trotzdem gearbeitet und Token verbraucht, bevor der
        Klient den Strom abbrach. Nur die BUCHUNG als `upstream_error` waere
        die falsche Aussage — aus der Kappe herausfallen darf der Verbrauch
        deswegen nicht.
        """
        db = self._require()
        row = db.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens"
            " FROM broker_ledger"
            " WHERE principal=? AND at>=? AND"
            " outcome IN ('forwarded','upstream_error','client_aborted')",
            (principal, float(since))).fetchone()
        return {"requests": int(row["requests"]), "tokens": int(row["tokens"])}

    #: Dieselbe Ausgangsmenge wie `usage_since` — was der Anbieter gearbeitet hat.
    USAGE_OUTCOMES = ("forwarded", "upstream_error", "client_aborted")

    def usage_for_task(self, task_ref: str, *, since: float) -> dict[str, Any]:
        """Was EIN Auftrag (Lease-`ref`) seit `since` verbraucht hat — und wie
        seine Zeilen ausgingen.

        Token werden ueber dieselbe Ausgangsmenge wie `usage_since` summiert
        (`forwarded`, `upstream_error`, `client_aborted`); die Verteilung
        (`outcomes`, `status_codes`, `rows`) zaehlt dagegen JEDE Zeile des
        Auftrags, Ablehnungen eingeschlossen — nur so laesst sich ein
        bewiesener Nichtstart (ausschliesslich 429 ohne Ausgabetoken) von
        einem gestarteten Turn unterscheiden. Eine Lesefunktion; kein
        zweiter Kostenspeicher.
        """
        db = self._require()
        ref = str(task_ref or "")
        totals = db.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens"
            " FROM broker_ledger WHERE task_ref=? AND at>=? AND"
            " outcome IN ('forwarded','upstream_error','client_aborted')",
            (ref, float(since))).fetchone()
        outcomes: dict[str, int] = {}
        statuses: dict[str, int] = {}
        rows = 0
        for row in db.execute(
                "SELECT outcome, status_code, COUNT(*) AS n FROM broker_ledger"
                " WHERE task_ref=? AND at>=? GROUP BY outcome, status_code",
                (ref, float(since))).fetchall():
            n = int(row["n"])
            rows += n
            outcomes[str(row["outcome"])] = outcomes.get(str(row["outcome"]), 0) + n
            code = str(int(row["status_code"]))
            statuses[code] = statuses.get(code, 0) + n
        return {"task_ref": ref, "since": float(since), "rows": rows,
                "requests": int(totals["requests"]),
                "input_tokens": int(totals["input_tokens"]),
                "output_tokens": int(totals["output_tokens"]),
                "tokens": int(totals["input_tokens"]) + int(totals["output_tokens"]),
                "outcomes": outcomes, "status_codes": statuses}

    def _trim(self) -> None:
        db = self._require()
        row = db.execute("SELECT COUNT(*) AS n FROM broker_ledger").fetchone()
        if int(row["n"]) <= MAX_ROWS:
            return
        db.execute(
            "DELETE FROM broker_ledger WHERE id NOT IN"
            " (SELECT id FROM broker_ledger ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
        db.commit()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Additive Spalten fuer ein Buch, das schon existiert.

        `CREATE TABLE IF NOT EXISTS` legt an einer vorhandenen Tabelle nichts
        nach. Ein Buch aus der Zeit vor `request_sha256` bekommt die Spalte
        also hier — leer fuer alle Altzeilen, und das ist die ehrliche Aussage:
        fuer sie wurde nie ein Hash gebildet. Nichts wird umgeschrieben, nichts
        umgezogen, der Beschnitt bleibt unveraendert.
        """
        have = {str(row["name"]) for row in
                db.execute("PRAGMA table_info(broker_ledger)").fetchall()}
        if "request_sha256" not in have:
            db.execute("ALTER TABLE broker_ledger"
                       " ADD COLUMN request_sha256 TEXT NOT NULL DEFAULT ''")
            log.info("broker.ledger_migrated", column="request_sha256")

    def _require(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("ledger is not open")
        return self._db
