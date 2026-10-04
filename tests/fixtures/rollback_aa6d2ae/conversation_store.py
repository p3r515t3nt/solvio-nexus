"""M1 — der Gespraechsspeicher, der SOLVIO gehoert.

WARUM ES IHN GIBT

Reproduziert vor dieser Aenderung: der Nutzertext aus
`conversation.item.input_audio_transcription.completed` wurde nur fuer die
Silent-Stop-Pruefung angesehen, der Assistententext aus
`response.output_audio_transcript.done` nur nach stdout gedruckt — beides danach
verworfen. Nach dem 30-Sekunden-Inaktivitaets-Timeout oeffnete das naechste Weckwort
eine neue Provider-Sitzung, die genau zwei `session.update`-Rahmen bekam und *null*
`conversation.item.create`. Das Gespraech war weg, weil SOLVIO es nie besessen hat.

WAS HIER STEHT — UND WAS NICHT

Gespeichert wird ausschliesslich finalisierter Gespraechstext: was der Nutzer gesagt hat
und was SOLVIO geantwortet hat. NICHT gespeichert werden Audio, PCM, Provider-Rahmen,
Zugangsdaten, Header, Tool-Nutzlasten oder System-Anweisungen.

Das ist KEIN semantisches Langzeitgedaechtnis. Es ist die lokale, kanonische Historie
eines laufenden Gespraechs. Eine Aufbewahrungsfrist ist in M1 bewusst NICHT festgelegt —
das ist eine Datenschutzentscheidung fuer spaeter. `delete_conversation` und `purge_all`
stehen bereit, damit diese Entscheidung nicht an fehlenden Primitiven scheitert.

SQLITE-DISZIPLIN

Wie die uebrigen Stores des Hauses: eigene Datei, `isolation_level=None` (Autocommit),
WAL, `synchronous=FULL`, `busy_timeout`, Verzeichnis 0700 und Datei 0600. Die Datei liegt
bewusst NICHT unter `~/.solvio-approvals` — dort liegt der eingefrorene Sicherheitszustand,
und Produktdaten haben darin nichts verloren.

ZEIT IST INJIZIERBAR

Jede Zeitentscheidung laeuft ueber `now_fn`. Kein Test muss 15 Minuten warten.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import stat
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_LINGER_SECONDS = 15 * 60.0
# M2: Wie lange der WOERTLICHE Gespraechsverlauf lokal liegen bleibt. Das ist etwas
# anderes als das Linger-Fenster: Linger entscheidet, was noch AKTIVER Kontext ist,
# Retention entscheidet, was ueberhaupt noch gespeichert bleibt. Dauerhaftes
# Gedaechtnis ist davon nicht betroffen — es liegt in memory.sqlite3.
DEFAULT_RETENTION_DAYS = 90.0
DEFAULT_CONTEXT_CHARS = 3000
DB_FILENAME = "conversations.sqlite3"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
_ROLES = (ROLE_USER, ROLE_ASSISTANT)

STATUS_ACTIVE = "active"


class ConversationStoreError(RuntimeError):
    """Der Speicher konnte seine Zusage nicht halten. Nie stillschweigend schlucken."""


def state_dir() -> str:
    """Produkt-Zustandsverzeichnis. Getrennt vom Sicherheitszustand, per Umgebung setzbar."""
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def default_db_path() -> str:
    return os.path.join(state_dir(), DB_FILENAME)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id  TEXT PRIMARY KEY,
    created_at       REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_conv_activity ON conversations(last_activity_at DESC);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    started_at      REAL NOT NULL,
    ended_at        REAL,
    close_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sess_conv ON conversation_sessions(conversation_id);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
    sequence          INTEGER NOT NULL,
    role              TEXT NOT NULL,
    text              TEXT NOT NULL,
    created_at        REAL NOT NULL,
    source_session_id TEXT,
    source_turn_id    TEXT,
    UNIQUE(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_seq ON conversation_messages(conversation_id, sequence);
"""


class ConversationStore:
    """Synchron und klein. Wer ihn vom Event-Loop fernhalten muss, tut das ausserhalb."""

    def __init__(self, path: str | None = None, *,
                 now_fn: Callable[[], float] = time.time,
                 linger_seconds: float = DEFAULT_LINGER_SECONDS,
                 retention_days: float = DEFAULT_RETENTION_DAYS) -> None:
        self.path = os.path.abspath(path or default_db_path())
        self.now = now_fn
        self.linger_seconds = float(linger_seconds)
        self.retention_days = float(retention_days)
        self._conn: sqlite3.Connection | None = None

    # ----------------------------------------------------------------- Lebenszyklus
    def open(self) -> "ConversationStore":
        directory = os.path.dirname(self.path)
        Path(directory).mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass                       # ein fremdes Verzeichnis ist kein Grund, nicht zu starten
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None,
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"conversation store unusable: {exc}") from exc
        # Traege Bereinigung genau hier: beim Oeffnen. Kein Zeitplaner, kein Dienst —
        # der Core startet ohnehin regelmaessig, und ein Purge, der nur laeuft wenn
        # jemand hinsieht, ist ehrlicher als einer, der im Hintergrund raten muss.
        # Ein fehlgeschlagener Purge darf den Start nicht kippen — aber er darf auch nicht
        # verschwinden. Frueher wurde die Ausnahme stillschweigend geschluckt; dann sieht
        # niemand, dass die Aufbewahrungsregel seit Wochen nicht mehr greift.
        self.last_purge: dict | None = None
        self.last_purge_error: str | None = None
        try:
            self.last_purge = self.purge_expired()
        except ConversationStoreError as exc:
            self.last_purge_error = f"{type(exc).__name__}: {exc}"
            print(f"[WARN] Gespraechs-Purge fehlgeschlagen: {self.last_purge_error}",
                  file=sys.stderr, flush=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ConversationStoreError("conversation store is not open")
        return self._conn

    # ----------------------------------------------------------------- Gespraeche
    def begin_session(self, session_id: str) -> tuple[str, bool]:
        """Eine Sprachsitzung an ein Gespraech binden.

        Gab es kuerzlich — innerhalb des Linger-Fensters — Aktivitaet, wird DIESES Gespraech
        fortgesetzt. Sonst beginnt ein neues. Der 30-Sekunden-Timeout des Providers beendet
        also die Sitzung, nicht das Gespraech.

        Gibt (conversation_id, resumed) zurueck.
        """
        now = self.now()
        try:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT conversation_id FROM conversations "
                    "WHERE status = ? AND last_activity_at >= ? "
                    "ORDER BY last_activity_at DESC LIMIT 1",
                    (STATUS_ACTIVE, now - self.linger_seconds)).fetchone()
                resumed = row is not None
                if resumed:
                    conversation_id = row["conversation_id"]
                    self.conn.execute(
                        "UPDATE conversations SET last_activity_at = ? WHERE conversation_id = ?",
                        (now, conversation_id))
                else:
                    conversation_id = "c-" + secrets.token_hex(8)
                    self.conn.execute(
                        "INSERT INTO conversations (conversation_id, created_at, "
                        "last_activity_at, status) VALUES (?, ?, ?, ?)",
                        (conversation_id, now, now, STATUS_ACTIVE))
                self.conn.execute(
                    "INSERT OR REPLACE INTO conversation_sessions "
                    "(session_id, conversation_id, started_at) VALUES (?, ?, ?)",
                    (session_id, conversation_id, now))
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"begin_session failed: {exc}") from exc
        return conversation_id, resumed

    def end_session(self, session_id: str, close_reason: str) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE conversation_sessions SET ended_at = ?, close_reason = ? "
                    "WHERE session_id = ?", (self.now(), close_reason, session_id))
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"end_session failed: {exc}") from exc

    # ----------------------------------------------------------------- Nachrichten
    def add_message(self, conversation_id: str, role: str, text: str, *,
                    source_session_id: str | None = None,
                    source_turn_id: str | None = None,
                    message_id: str | None = None) -> str:
        """Eine finalisierte Gespraechsnachricht festhalten.

        Nur `user` und `assistant`. Andere Rollen — insbesondere `system` — gehoeren nicht
        in die Historie: sie wuerden beim Wiedereinspielen zu Anweisungen hoeherer
        Autoritaet werden.
        """
        if role not in _ROLES:
            raise ConversationStoreError(f"refusing to store role {role!r}")
        text = (text or "").strip()
        if not text:
            return ""
        # Der Zaun des Tresors — hier REDIGIEREND, nicht verweigernd.
        #
        # Dieser Speicher ist ein Verlauf, kein Gedaechtnis. Einen ganzen
        # Redebeitrag zu verwerfen, weil ein Passwort darin steht, wuerde die
        # Gespraechshoheit brechen: der Core besitzt das Gespraech, und eine
        # Luecke ohne Vermerk waere eine Unwahrheit ueber das, was gesagt wurde.
        # Der Beitrag bleibt also stehen; sein Wert nicht. Neunzig Tage Klartext
        # (`DEFAULT_RETENTION_DAYS`) sind der Grund, warum das hier und nicht
        # eine Schicht hoeher steht.
        from solvio.secret_vault.firewall import redact_if_credential
        text = redact_if_credential(text, where="conversation.add_message")
        now = self.now()
        # M2: der Aufrufer darf die Kennung mitbringen. Die Sprachschicht braucht sie
        # SOFORT — sie muss die Merk-Absicht mit der Nachricht verknuepfen, aus der sie
        # stammt, waehrend der Schreibvorgang noch in der Warteschlange liegt. Ohne das
        # blieb `source_message_id` in der Provenienz leer.
        message_id = message_id or ("m-" + secrets.token_hex(8))
        try:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS s FROM conversation_messages "
                    "WHERE conversation_id = ?", (conversation_id,)).fetchone()
                self.conn.execute(
                    "INSERT INTO conversation_messages (message_id, conversation_id, sequence, "
                    "role, text, created_at, source_session_id, source_turn_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (message_id, conversation_id, int(row["s"]) + 1, role, text, now,
                     source_session_id, source_turn_id))
                self.conn.execute(
                    "UPDATE conversations SET last_activity_at = ? WHERE conversation_id = ?",
                    (now, conversation_id))
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"add_message failed: {exc}") from exc
        return message_id

    def recent_context(self, conversation_id: str, *,
                       max_chars: int = DEFAULT_CONTEXT_CHARS) -> list[dict[str, Any]]:
        """Das juengste Fenster des Gespraechs, in chronologischer Reihenfolge.

        Deterministisch und woertlich: keine Zusammenfassung, kein zweites Modell. Passt
        eine Nachricht nicht mehr ins Budget, faellt sie GANZ weg — es wird nicht mitten in
        einem Satz oder gar in einem UTF-8-Zeichen geschnitten. Aeltestes faellt zuerst.
        """
        try:
            rows = self.conn.execute(
                "SELECT role, text, sequence FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY sequence DESC", (conversation_id,)).fetchall()
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"recent_context failed: {exc}") from exc
        picked: list[dict[str, Any]] = []
        used = 0
        for row in rows:                          # neueste zuerst einsammeln
            length = len(row["text"])
            if used + length > max_chars:
                break                             # aelteres passt erst recht nicht
            picked.append({"role": row["role"], "text": row["text"],
                           "sequence": row["sequence"]})
            used += length
        picked.reverse()                          # dann chronologisch ausliefern
        return picked

    def message(self, conversation_id: str, message_id: str) -> dict[str, Any] | None:
        """One already persisted message, bound to its original conversation."""
        row = self.conn.execute(
            "SELECT message_id, role, text, source_session_id, source_turn_id "
            "FROM conversation_messages WHERE conversation_id=? AND message_id=?",
            (conversation_id, message_id)).fetchone()
        return dict(row) if row else None

    def messages(self, conversation_id: str, *,
                 limit: int = 0) -> list[dict[str, Any]]:
        """Die Nachrichten einer Konversation, chronologisch.

        `limit > 0` liest nur die juengsten und gibt sie trotzdem chronologisch
        zurueck. Der Grund ist der Sprachweg: seit der Router den vorigen
        Nutzer-Turn bindet, laeuft dieser Leseweg in JEDEM Turn statt nur nach
        einer Rueckfrage. Ein voller Tabellenlauf pro gesprochenem Satz waere
        eine Latenzschuld, die mit dem Gespraech waechst.
        """
        if int(limit) > 0:
            rows = self.conn.execute(
                "SELECT message_id, sequence, role, text, created_at, "
                "source_session_id, source_turn_id FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY sequence DESC LIMIT ?",
                (conversation_id, int(limit))).fetchall()
            return [dict(r) for r in reversed(rows)]
        rows = self.conn.execute(
            "SELECT message_id, sequence, role, text, created_at, source_session_id, "
            "source_turn_id FROM conversation_messages WHERE conversation_id = ? "
            "ORDER BY sequence", (conversation_id,)).fetchall()
        return [dict(r) for r in rows]

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,)).fetchone()
        return dict(row) if row else None

    def sessions_of(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM conversation_sessions WHERE conversation_id = ? ORDER BY started_at",
            (conversation_id,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- Loeschen
    def delete_conversation(self, conversation_id: str) -> int:
        """Ein Gespraech restlos entfernen. Deterministisch, ohne Zeitplan und ohne UI —
        die Aufbewahrungsentscheidung faellt spaeter, das Primitiv soll dann dastehen."""
        try:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                n = self.conn.execute(
                    "DELETE FROM conversation_messages WHERE conversation_id = ?",
                    (conversation_id,)).rowcount
                self.conn.execute("DELETE FROM conversation_sessions WHERE conversation_id = ?",
                                  (conversation_id,))
                self.conn.execute("DELETE FROM conversations WHERE conversation_id = ?",
                                  (conversation_id,))
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"delete_conversation failed: {exc}") from exc
        return n

    def purge_expired(self, *, now: float | None = None) -> dict:
        """Woertlichen Gespraechsverlauf entfernen, der aelter als die Aufbewahrungsfrist ist.

        Entfernt werden GESPRAECHE samt ihren Nachrichten und Sitzungszeilen — nicht
        einzelne Nachrichten aus einem noch laufenden Gespraech, denn ein halb
        entkerntes Gespraech waere schlimmer als ein ganzes oder gar keines.

        Dauerhaftes Gedaechtnis liegt in einer anderen Datenbank und wird hiervon NICHT
        beruehrt. Es gibt bewusst keine Kaskade.
        """
        moment = self.now() if now is None else now
        cutoff = moment - self.retention_days * 86400.0
        try:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                rows = self.conn.execute(
                    "SELECT conversation_id FROM conversations WHERE last_activity_at < ?",
                    (cutoff,)).fetchall()
                ids = [r["conversation_id"] for r in rows]
                messages = 0
                for conversation_id in ids:
                    messages += self.conn.execute(
                        "DELETE FROM conversation_messages WHERE conversation_id = ?",
                        (conversation_id,)).rowcount
                    self.conn.execute(
                        "DELETE FROM conversation_sessions WHERE conversation_id = ?",
                        (conversation_id,))
                    self.conn.execute(
                        "DELETE FROM conversations WHERE conversation_id = ?",
                        (conversation_id,))
        except sqlite3.Error as exc:
            raise ConversationStoreError(f"purge_expired failed: {exc}") from exc
        return {"conversations": len(ids), "messages": messages,
                "retention_days": self.retention_days}

    def purge_all(self) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for table in ("conversation_messages", "conversation_sessions", "conversations"):
                self.conn.execute(f"DELETE FROM {table}")
