"""Wo SOLVIO festhaelt, welchen Anruf es begonnen hat — und wie er ausging.

Das dauerhafte Ausfuehrungsjournal des Freigabewegs (`execution_attempts` in
`approval_control.sqlite3`) beantwortet die Frage "durfte das passieren und ist
die Grenze ueberschritten worden". Es hat ausdruecklich KEINE Spalte fuer eine
anbieterseitige Kennung, und es ist waehrend `EXTERNAL_PENDING` gegen
Zusatzinformation verschlossen. Genau deshalb gibt es diese Datei — dieselbe
Aufteilung, die die Zahlung getroffen hat, und aus demselben Grund.

Die Reihenfolge ist der ganze Zweck:

    Die Zeile entsteht VOR dem Draht.

Wer erst anruft und dann aufschreibt, verliert bei einem Absturz genau
dazwischen den Faden zum Anbieter — und damit die Antwort auf die einzige Frage,
die nach einem Neustart zaehlt: hat es geklingelt, und was ist daraus geworden?
Die `conversation_id` wird nachgetragen, sobald der Anbieter sie nennt; bis
dahin steht die Zeile mit leerem Faden da und sagt ehrlich: hier wurde etwas
begonnen, dessen Ausgang wir noch nicht kennen.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

DEFAULT_PATH = "~/.solvio/telephony.sqlite3"
PATH_ENV = "SOLVIO_TELEPHONY_DB"


class CallLedger:
    """Ein Anruf je `execution_id`. Der Primaerschluessel ist die Klammer.

    `execution_id` stammt aus `execution_id_for(core_instance_id, approval_id)`
    und ist eine reine Funktion von Core und Freigabe. Sie ueberlebt jeden
    Neustart und wird bei einem Wiederholungsversuch NICHT neu gepraegt —
    deshalb kann sie hier Primaerschluessel sein, und deshalb kann eine zweite
    Ausfuehrung derselben Freigabe hier keine zweite Zeile erzeugen.
    """

    def __init__(self, path: str = "") -> None:
        chosen = path or os.environ.get(PATH_ENV, "") or DEFAULT_PATH
        self.path = os.path.abspath(os.path.expanduser(chosen))
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        os.chmod(self.path, 0o600)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                execution_id   TEXT PRIMARY KEY,
                call_id        TEXT NOT NULL,
                approval_id    TEXT NOT NULL DEFAULT '',
                provider       TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                recipient_alias TEXT NOT NULL,
                recipient_display TEXT NOT NULL DEFAULT '',
                recipient_handle TEXT NOT NULL,
                bound_message  TEXT NOT NULL,
                objective      TEXT NOT NULL DEFAULT '',
                max_duration_secs INTEGER NOT NULL DEFAULT 0,
                call_state     TEXT NOT NULL,
                delivery_state TEXT NOT NULL,
                started_at     REAL,
                ended_at       REAL,
                transcript_summary TEXT NOT NULL DEFAULT '',
                recipient_response TEXT NOT NULL DEFAULT '',
                cost_credits   INTEGER,
                cost_fiat      REAL,
                duration_secs  INTEGER,
                provider_result TEXT NOT NULL DEFAULT '',
                created_at     REAL NOT NULL,
                updated_at     REAL NOT NULL)
        """)
        # Ein Faden, ein Anruf. Ohne diesen Index koennte derselbe
        # Anbietervorgang zwei Zeilen bekommen und die Wiederaufnahme haette
        # zwei Wahrheiten. Leere Faeden sind ausgenommen: davon gibt es
        # notwendigerweise mehrere, solange der Anbieter noch nicht geantwortet hat.
        self._db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS calls_conversation
            ON calls(conversation_id) WHERE conversation_id != ''
        """)

    # -- Schreiben ---------------------------------------------------------
    def prepare(self, *, execution_id: str, call_id: str, approval_id: str,
                provider: str, recipient_alias: str, recipient_display: str,
                recipient_handle: str, bound_message: str, objective: str,
                max_duration_secs: int, call_state: str,
                delivery_state: str) -> None:
        """Die Zeile VOR dem Draht. Idempotent: ein zweiter Anlauf legt nichts Neues an."""
        now = time.time()
        self._db.execute("""
            INSERT INTO calls (execution_id, call_id, approval_id, provider,
                recipient_alias, recipient_display, recipient_handle,
                bound_message, objective, max_duration_secs,
                call_state, delivery_state, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(execution_id) DO NOTHING
        """, (execution_id, call_id, approval_id, provider, recipient_alias,
              recipient_display, recipient_handle, bound_message, objective,
              int(max_duration_secs), call_state, delivery_state, now, now))

    def attach_conversation(self, execution_id: str, conversation_id: str) -> bool:
        """Traegt den Faden nach, sobald der Anbieter ihn genannt hat.

        Bewusst nur, solange er leer ist: ein einmal gesetzter Faden wird nie
        ueberschrieben. Sonst koennte ein zweiter Anlauf den Ausgang des ersten
        unauffindbar machen.

        Gibt `False` zurueck, wenn der Faden bereits einer ANDEREN Ausfuehrung
        gehoert. Das ist kein Absturz und darf keiner sein: an dieser Stelle ist
        gerade gewaehlt worden, das Telefon klingelt moeglicherweise, und eine
        ungefangene Ausnahme wuerde den Vorgang genau hier zerreissen — mit
        einer Ledgerzeile, die noch auf PREPARED steht. Der Aufrufer erfaehrt
        den Konflikt und behandelt ihn als mehrdeutig.
        """
        try:
            cursor = self._db.execute(
                "UPDATE calls SET conversation_id=?, updated_at=? "
                "WHERE execution_id=? AND conversation_id=''",
                (conversation_id, time.time(), execution_id))
        except sqlite3.IntegrityError:
            return False
        if cursor.rowcount:
            return True
        # Kein Treffer: entweder steht der Faden schon (derselbe Anlauf) oder
        # die Zeile fehlt. Nur der erste Fall ist in Ordnung.
        zeile = self.get(execution_id)
        return bool(zeile and zeile["conversation_id"] == conversation_id)

    def record_outcome(self, execution_id: str, *, call_state: str,
                       delivery_state: str, started_at: float | None = None,
                       ended_at: float | None = None,
                       transcript_summary: str = "", recipient_response: str = "",
                       cost_credits: int | None = None,
                       cost_fiat: float | None = None,
                       duration_secs: int | None = None,
                       provider_result: Any = None) -> None:
        self._db.execute("""
            UPDATE calls SET call_state=?, delivery_state=?, started_at=?,
                ended_at=?, transcript_summary=?, recipient_response=?,
                cost_credits=?, cost_fiat=?, duration_secs=?,
                provider_result=?, updated_at=?
            WHERE execution_id=?
        """, (call_state, delivery_state, started_at, ended_at,
              transcript_summary[:4000], recipient_response[:4000],
              cost_credits, cost_fiat, duration_secs,
              json.dumps(provider_result, ensure_ascii=False)[:8000]
              if provider_result is not None else "",
              time.time(), execution_id))

    # -- Lesen -------------------------------------------------------------
    def get(self, execution_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM calls WHERE execution_id=?",
                               (execution_id,)).fetchone()
        return dict(row) if row is not None else None

    def by_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        row = self._db.execute("SELECT * FROM calls WHERE conversation_id=?",
                               (conversation_id,)).fetchone()
        return dict(row) if row is not None else None

    def unfinished(self, terminal_states: frozenset[str]) -> list[dict[str, Any]]:
        """Anrufe, deren Ausgang offen ist — der Einstieg nach einem Neustart.

        Nur solche mit Faden: ohne `conversation_id` gibt es beim Anbieter
        nichts nachzulesen, und Raten ist keine Wiederaufnahme.
        """
        if not terminal_states:
            return []
        platzhalter = ",".join("?" * len(terminal_states))
        rows = self._db.execute(
            f"SELECT * FROM calls WHERE conversation_id != '' "
            f"AND call_state NOT IN ({platzhalter}) ORDER BY created_at",
            tuple(sorted(terminal_states))).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
