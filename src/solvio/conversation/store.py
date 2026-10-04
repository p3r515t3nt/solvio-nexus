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

N8/C3 — DAUERHAFTE CHATS

Seit C3 traegt derselbe Speicher auch die expliziten Text-Chats aus Dashboard und
iPhone-App: `conversations` bekommt `owner_principal`, `title`, `kind`, `explicit`
(additiv, idempotent nachgezogen), und drei neue Tabellen halten Zustellungen
(`conversation_deliveries`), Auftragsverweise (`conversation_task_links`) und die
Idempotenz des Anlegens (`conversation_creations`). Kein zweiter Speicher, keine
zweite Verbindung — und der Sprachweg merkt fast nichts: `begin_session` ohne
Argumente, `messages`/`recent_context` ohne Sequenzgrenze verhalten sich
bytegleich zu vorher; `add_message` legt seit C3 ueber JEDE Verlaufszeile (auch
Sprachtranskripte) zusaetzlich den Zeilenzaun (Review Runde 15, B15-1 / Runde 18,
C18-H2): die Heuristik des Gedaechtnisses wie zuvor, dann Schluesselformen,
Zuweisungen und Aussagen mit Wert.

SQLITE-DISZIPLIN

Wie die uebrigen Stores des Hauses: eigene Datei, `isolation_level=None` (Autocommit),
WAL, `synchronous=FULL`, `busy_timeout`, Verzeichnis 0700 und Datei 0600. Die Datei liegt
bewusst NICHT unter `~/.solvio-approvals` — dort liegt der eingefrorene Sicherheitszustand,
und Produktdaten haben darin nichts verloren.

Eine Verbindung, mehrere Threads (Sprachweg via `asyncio.to_thread`, Endpunkt und
Prozessor ebenso): jede oeffentliche Methode — auch jeder Leser — haelt `_lock`.
Ein Leser wartet damit, bis eine offene Schreibtransaktion beendet ist; er liest nie
in eine fremde Transaktion hinein. Wer MEHRERE Leser zu einer Sicht zusammensetzt,
haelt den Lock ueber alle mit `with store.reading():` — sonst kann ein Abschluss
zwischen zwei Abfragen commiten und die Sicht ist zerrissen.

ZEIT IST INJIZIERBAR

Jede Zeitentscheidung laeuft ueber `now_fn`. Kein Test muss 15 Minuten warten.
"""
from __future__ import annotations

import os
import json
from contextlib import contextmanager
import math
import secrets
import sqlite3
import sys
import stat
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from solvio.secret_vault.firewall import TRANSCRIPT_MARKER

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

# C3: Arten und Zustaende der expliziten Chats.
KIND_VOICE = "voice"
KIND_TEXT = "text"
DELIVERY_ACCEPTED = "accepted"
DELIVERY_RUNNING = "running"
DELIVERY_COMPLETED = "completed"
DELIVERY_BLOCKED = "blocked"
DELIVERY_OPEN = (DELIVERY_ACCEPTED, DELIVERY_RUNNING)
DELIVERY_SOURCE_KINDS = ("app", "dashboard")
#: Ein leerer expliziter Chat, der so lange ohne Nachricht bleibt, wird beim
#: naechsten Purge-Lauf aufgeraeumt (§1.3 des C3-Vertrags).
EMPTY_EXPLICIT_HYGIENE_SECONDS = 24 * 3600.0
#: Titel: aus dem ersten Nutzertext abgeleitet (≤ 60 Zeichen), oder explizit
#: gesetzt (1..80 Zeichen, `PATCH`).
TITLE_DERIVED_CHARS = 60
MAX_TITLE_CHARS = 80
_ELLIPSIS = "…"
# C6 reads the canonical history directly. These are work/output budgets,
# not a second index or a restriction to the newest hundred conversations.
CONTEXT_SCAN_ROWS = 4096
#: Je Gespraech, damit ein gespraechiger Chat keinen aelteren aus der Reichweite
#: draengt (DEBT-0309; gemessen in Review Runde 22: das ZEILENbudget bindet bei
#: normaler Nachrichtenlaenge lange vor dem Zeichenbudget).
CONTEXT_SCAN_ROWS_PER_CHAT = 256
CONTEXT_SCAN_CHARS = 1_000_000
#: „Juengste Auftragsverknuepfungen" ist eine Aussage ueber Zeit: ein Jahre alter
#: Verweis gehoert nicht in den Rueckfallweg, auch wenn er der neueste ist
#: (DEBT-0308; ein Thementreffer darf dagegen beliebig alt sein — er traegt Evidenz).
RECENT_LINK_MAX_AGE_SECONDS = 30 * 24 * 3600.0
CONTEXT_SQL_STEPS = 300_000
CONTEXT_MESSAGE_CHARS = 8192
CONTEXT_QUERY_CHARS = 2000
CONTEXT_MAX_HITS = 20
CONTEXT_EXCERPT_CHARS = 400
CONTEXT_LINKS_PER_HIT = 6


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

# C3 — zusaetzlich, hinter `_SCHEMA` im selben executescript. Alle drei Tabellen
# referenzieren `conversations` unter `PRAGMA foreign_keys=ON`: eine Gespraechszeile
# mit Kindzeilen faellt nur, wenn die Kindzeilen zuerst fallen
# (`_delete_conversation_rows`).
_SCHEMA_C3 = """
CREATE TABLE IF NOT EXISTS conversation_deliveries (
    delivery_id          TEXT PRIMARY KEY,
    conversation_id      TEXT NOT NULL REFERENCES conversations(conversation_id),
    client_message_id    TEXT NOT NULL,
    principal            TEXT NOT NULL,
    source_kind          TEXT NOT NULL CHECK(source_kind IN ('app','dashboard')),
    source_ref           TEXT NOT NULL,
    device_id            TEXT NOT NULL DEFAULT '',
    core_id              TEXT NOT NULL,
    source_generation    TEXT NOT NULL,
    digest               TEXT NOT NULL,
    status               TEXT NOT NULL CHECK(status IN ('accepted','running','completed','blocked')),
    worker_generation    TEXT NOT NULL DEFAULT '',
    message_id           TEXT NOT NULL,
    message_sequence     INTEGER NOT NULL,
    attachments          TEXT NOT NULL DEFAULT '',
    target               TEXT NOT NULL DEFAULT '',
    dispatch             TEXT NOT NULL DEFAULT '',
    assistant_message_id TEXT NOT NULL DEFAULT '',
    task_id              TEXT NOT NULL DEFAULT '',
    run_id               TEXT NOT NULL DEFAULT '',
    revision             INTEGER NOT NULL DEFAULT 0,
    activity_id          TEXT NOT NULL DEFAULT '',
    error_code           TEXT NOT NULL DEFAULT '',
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    UNIQUE(conversation_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS idx_deliv_conv ON conversation_deliveries(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_deliv_open ON conversation_deliveries(status) WHERE status IN ('accepted','running');

CREATE TABLE IF NOT EXISTS conversation_task_links (
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    task_id         TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    linked_at       REAL NOT NULL,
    source          TEXT NOT NULL,
    PRIMARY KEY(conversation_id, task_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_links_task ON conversation_task_links(task_id);

CREATE TABLE IF NOT EXISTS conversation_creations (
    principal         TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
    created_at        REAL NOT NULL,
    PRIMARY KEY(principal, client_request_id)
);
"""

#: Die C3-Spalten an `conversations` — additiv, jede mit Vorgabe, damit die 182
#: historischen, ownerlosen Sprachgespraeche unveraendert bleiben
#: (`owner_principal=''`, `kind='voice'`, `explicit=0`).
_CONVERSATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("owner_principal", "TEXT NOT NULL DEFAULT ''"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("kind", "TEXT NOT NULL DEFAULT 'voice'"),
    ("explicit", "INTEGER NOT NULL DEFAULT 0"),
    ("room_device_id", "TEXT NOT NULL DEFAULT ''"),
    ("room_session_id", "TEXT NOT NULL DEFAULT ''"),
)

#: Die Loeschreihenfolge der Kindzeilen — Kinder vor Eltern, Fremdschluessel bleiben an.
_CHILD_TABLES: tuple[str, ...] = (
    "conversation_deliveries", "conversation_task_links", "conversation_creations",
    "conversation_messages", "conversation_sessions",
)


def _migrate_conversations(conn: sqlite3.Connection) -> list[str]:
    """Fehlende C3-Spalten an `conversations` nachziehen — idempotent, additiv.

    Vorbild ist `agent_runtime/task_start_service.py::_migrate_sources`:
    `PRAGMA table_info`, fehlende Spalte → `ALTER TABLE ADD COLUMN ... DEFAULT`,
    alles unter genau einem `BEGIN IMMEDIATE`. Es wird keine Zeile umgeschrieben,
    keine Kennung neu vergeben und nichts entfernt. Gibt die nachgezogenen
    Spaltennamen zurueck (leer beim zweiten Oeffnen).
    """
    added: list[str] = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        known = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        for name, declaration in _CONVERSATION_COLUMNS:
            if name not in known:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {declaration}")
                added.append(name)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_owner "
                     "ON conversations(owner_principal, last_activity_at DESC)")
    return added


def derive_title(text: str) -> str:
    """Der Titel eines Chats aus dem ersten Nutzertext (§1.4).

    Whitespace zu Leerzeichen kollabiert, an Wortgrenze auf hoechstens
    `TITLE_DERIVED_CHARS` Zeichen gekuerzt, bei Kuerzung „…" angehaengt (im Budget
    enthalten). Kein Modellaufruf. Der Aufrufer reicht den bereits redigierten,
    persistierten Text herein — nie den Request-Body.
    """
    words = " ".join(str(text or "").split())
    if len(words) <= TITLE_DERIVED_CHARS:
        return words
    budget = TITLE_DERIVED_CHARS - len(_ELLIPSIS)
    cut = words[:budget]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + _ELLIPSIS


def _clean_title(title: str) -> str:
    """Ein explizit gesetzter Titel: redigiert, kollabiert, 1..`MAX_TITLE_CHARS`."""
    from solvio.secret_vault.firewall import redact_if_credential, redact_lines_if_credential
    words = " ".join(str(title or "").split())
    # Dieselben zwei Zaeune wie fuer jede Verlaufszeile (Runde 14 B14-H1, Runde 15 R15-1).
    words = redact_if_credential(words, where="conversation.title")
    words = redact_lines_if_credential(words, where="conversation.title", chat=True)[0]
    if not words or len(words) > MAX_TITLE_CHARS:
        raise ConversationStoreError("invalid_title")
    return words


def _owned(row: Any, principal: str) -> bool:
    """Die eine Eigentumsregel: aktiv, explizit, dieser Principal, nicht leer."""
    return bool(row) and bool(principal) and (
        row["status"] == STATUS_ACTIVE and int(row["explicit"] or 0) == 1
        and row["owner_principal"] == principal)


def _context_fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _context_excerpt(text: str, needle: str, common: set[str], budget: int) -> str:
    """A bounded original-text window around a literal phrase or content word."""
    text = unicodedata.normalize("NFC", text)
    folded = text.casefold()
    position = folded.find(needle) if needle else -1
    if position < 0:
        positions = [folded.find(word) for word in common if word in folded]
        position = min(positions, default=0)
    # casefold may expand a character (Straße -> strasse); map back before
    # slicing so the excerpt remains source text, not a lowercased rewrite.
    offset = 0
    index = 0
    for index, char in enumerate(text):
        offset += len(char.casefold())
        if offset > position:
            break
    start = max(0, index - 60)
    prefix = _ELLIPSIS if start else ""
    room = max(0, budget - len(prefix))
    body = text[start:start + room]
    if len(text) > start + room and body:
        body = body[:-1] + _ELLIPSIS
    return (prefix + body)[:budget]


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
        # C3: EINE Verbindung, mehrere Threads. Jede oeffentliche Methode haelt ihn —
        # auch die Leser, damit keiner in eine offene Transaktion hineinliest.
        self._lock = threading.RLock()
        self.migrated_columns: list[str] = []

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
            self._conn.executescript(_SCHEMA + _SCHEMA_C3)
            self.migrated_columns = _migrate_conversations(self._conn)
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
        with self._lock:
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

    def reading(self):
        """Ein zusammenhaengender Lesevorgang: alle Leser darin sehen DENSELBEN Stand.

        Jeder oeffentliche Leser haelt `_lock` nur fuer seine eine Abfrage. Wer
        mehrere Leser zu einer Sicht zusammensetzt (die Detailroute: Eigentum,
        Gespraech, Nachrichten, Zustellungen, Verweise), bekommt sonst einen
        zerrissenen Stand, sobald ein `complete_delivery` dazwischen commitet —
        `completed` mit `assistant_message_id`, aber ohne die Assistentennachricht
        im Verlauf. Der Lock ist wiedereintrittsfaehig: die Leser darin nehmen ihn
        erneut, ohne zu warten. Nur fuer Store-Lesevorgaenge halten — nie ueber
        fremde Buecher oder Netz hinweg.
        """
        return self._lock

    # ----------------------------------------------------------------- Helfer (transaktionsfrei)
    #
    # Diese drei erwarten eine OFFENE Transaktion des Aufrufers und fuehren selbst
    # weder BEGIN noch COMMIT aus. `add_message` oeffnet seine eigene; Annahme und
    # Abschluss einer Zustellung (§2.3, §3.4) legen Nachricht und Zustellzeile unter
    # GENAU EINEM `BEGIN IMMEDIATE` ab — ein zweites BEGIN wuerfe, und ein innerer
    # Kontextmanager commitete die Nachricht, bevor die Zustellzeile folgt.

    @staticmethod
    def _require_room_source(conn: sqlite3.Connection, conversation_id: str,
                             session_id: str | None) -> None:
        """Room transcripts accept only sessions bound by the HMAC room path.

        Private clients may read the projection but cannot append, join by voice
        or attach a private task. Thus ongoing room context readers cannot later
        encounter a private continuation. Caller holds the write transaction.
        """
        row = conn.execute("SELECT room_device_id FROM conversations WHERE conversation_id=?",
                           (conversation_id,)).fetchone()
        if row is not None and row["room_device_id"]:
            if not session_id or not conn.execute(
                    "SELECT 1 FROM conversation_sessions WHERE conversation_id=? AND session_id=?",
                    (conversation_id, session_id)).fetchone():
                raise ConversationStoreError("room_conversation_read_only")

    def _insert_message(self, conn: sqlite3.Connection, conversation_id: str, role: str,
                        text: str, *, source_session_id: str | None,
                        source_turn_id: str | None, message_id: str | None,
                        now: float | None = None) -> tuple[str, int]:
        """Eine Nachricht unter der offenen Transaktion ablegen → (message_id, sequence).

        Redaktion und Sequenzvergabe exakt wie `add_message` vor C3.
        """
        self._require_room_source(conn, conversation_id, source_session_id)
        if role not in _ROLES:
            raise ConversationStoreError(f"refusing to store role {role!r}")
        text = (text or "").strip()
        if not text:
            raise ConversationStoreError("empty_message")
        # Der Zaun des Tresors — hier REDIGIEREND, nicht verweigernd.
        #
        # Dieser Speicher ist ein Verlauf, kein Gedaechtnis. Einen ganzen
        # Redebeitrag zu verwerfen, weil ein Passwort darin steht, wuerde die
        # Gespraechshoheit brechen: der Core besitzt das Gespraech, und eine
        # Luecke ohne Vermerk waere eine Unwahrheit ueber das, was gesagt wurde.
        # Der Beitrag bleibt also stehen; sein Wert nicht. Klartext im Verlauf
        # (Sprachgespraeche `DEFAULT_RETENTION_DAYS`, explizite Chats bis zum
        # Loeschen — DEBT-0265) ist der Grund, warum das hier und nicht eine
        # Schicht hoeher steht.
        from solvio.secret_vault.firewall import redact_if_credential, redact_lines_if_credential
        # ZWEI Zaeune, beide bleiben: (1) die fuer Sprach- und Merksaetze
        # kalibrierte Aussage-Heuristik des Gedaechtnisses (M2) — dieser
        # Schreibweg traegt auch jedes ASR-Transkript („mein passwort ist
        # sonnenblume", „die TAN ist 482913"); Review Runde 15 (B15-1/R15-1) hat
        # gemessen, dass ihr Wegfall zwoelf von achtzehn gesprochenen
        # Zugangsdaten des M2-Korpus in den Klartext liess — und (2) der
        # Zeilenzaun der Runden 9–14 (Zuweisungen, zitierte Werte mit
        # Leerzeichen, URL-Userinfo, `-u user:pw`, Aussagen mit Wert), den die
        # Heuristik nicht kennt (Runde 13, B13-1). Dass die Verbregel eine
        # getippte Wissensfrage („Was ist der Unterschied zwischen Basic Auth
        # und Bearer Tokens?") zum Marker macht, ist DEBT-0295 — eine
        # Entscheidung, keine Abschwaechung. Der Beitrag bleibt, sein Wert nicht.
        text = redact_if_credential(text, where="conversation.add_message")
        text = redact_lines_if_credential(text, where="conversation.add_message", chat=True)[0]
        moment = self.now() if now is None else now
        # M2: der Aufrufer darf die Kennung mitbringen. Die Sprachschicht braucht sie
        # SOFORT — sie muss die Merk-Absicht mit der Nachricht verknuepfen, aus der sie
        # stammt, waehrend der Schreibvorgang noch in der Warteschlange liegt. Ohne das
        # blieb `source_message_id` in der Provenienz leer.
        message_id = message_id or ("m-" + secrets.token_hex(8))
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM conversation_messages "
            "WHERE conversation_id = ?", (conversation_id,)).fetchone()
        sequence = int(row["s"]) + 1
        conn.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, sequence, "
            "role, text, created_at, source_session_id, source_turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, sequence, role, text, moment,
             source_session_id, source_turn_id))
        return message_id, sequence

    @staticmethod
    def _touch_activity(conn: sqlite3.Connection, conversation_id: str, now: float) -> None:
        conn.execute("UPDATE conversations SET last_activity_at = ? WHERE conversation_id = ?",
                     (now, conversation_id))

    @staticmethod
    def _delete_conversation_rows(conn: sqlite3.Connection, conversation_id: str) -> int:
        """Alle Zeilen eines Gespraechs — Kinder zuerst, dann die Gespraechszeile.

        Der EINE Loeschweg fuer `delete_conversation`, `purge_expired` und die
        Hygiene leerer Chats. Ohne ihn scheiterte `DELETE FROM conversations` an
        der Fremdschluesselpruefung, sobald ein Sprachgespraech einen
        `voice:`-Link traegt, die ganze Purge-Transaktion rollte zurueck — und die
        Aufbewahrungsregel waere fuer ALLE Gespraeche still ausser Kraft.
        Gibt die Zahl der geloeschten Nachrichten zurueck.
        """
        messages = 0
        for table in _CHILD_TABLES:
            removed = conn.execute(f"DELETE FROM {table} WHERE conversation_id = ?",
                                   (conversation_id,)).rowcount
            if table == "conversation_messages":
                messages = removed
        conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
        return messages

    # ----------------------------------------------------------------- Gespraeche
    def begin_session(self, session_id: str, *, conversation_id: str | None = None,
                      principal: str = "") -> tuple[str, bool]:
        """Eine Sprachsitzung an ein Gespraech binden.

        Ohne `conversation_id`: gab es kuerzlich — innerhalb des Linger-Fensters —
        Aktivitaet in einem NICHT expliziten Gespraech, wird DIESES fortgesetzt. Sonst
        beginnt ein neues (`kind='voice'`, `explicit=0`, `owner_principal=principal`).
        Der 30-Sekunden-Timeout des Providers beendet also die Sitzung, nicht das
        Gespraech. Die globale Anknuepfung waehlt nie einen expliziten Chat.

        Mit `conversation_id`: es wird NUR diese Zeile gebunden — aktiv, explizit und im
        Besitz von `principal`; ein leerer Principal bindet nie einen expliziten Chat.
        Sonst `ConversationStoreError("conversation_not_bindable")`.

        `principal` ist ausschliesslich der Principal aus einem Sitzungsbeweis
        (`app_task_session`/`browser_task_session`) — nie `satellite_id`.

        Gibt (conversation_id, resumed) zurueck.
        """
        now = self.now()
        principal = str(principal or "")
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    if conversation_id:
                        row = self.conn.execute(
                            "SELECT conversation_id, status, explicit, owner_principal, room_device_id "
                            "FROM conversations WHERE conversation_id = ?",
                            (conversation_id,)).fetchone()
                        if not _owned(row, principal) or row["room_device_id"]:
                            raise ConversationStoreError("conversation_not_bindable")
                        resumed = True
                        self.conn.execute(
                            "UPDATE conversations SET last_activity_at = ? WHERE conversation_id = ?",
                            (now, conversation_id))
                    else:
                        row = self.conn.execute(
                            "SELECT conversation_id FROM conversations "
                            "WHERE status = ? AND last_activity_at >= ? AND explicit = 0 "
                            "ORDER BY last_activity_at DESC LIMIT 1",
                            (STATUS_ACTIVE, now - self.linger_seconds)).fetchone()
                        resumed = row is not None
                        if resumed:
                            conversation_id = row["conversation_id"]
                            self.conn.execute(
                                "UPDATE conversations SET last_activity_at = ? "
                                "WHERE conversation_id = ?", (now, conversation_id))
                        else:
                            conversation_id = "c-" + secrets.token_hex(8)
                            self.conn.execute(
                                "INSERT INTO conversations (conversation_id, created_at, "
                                "last_activity_at, status, owner_principal, title, kind, "
                                "explicit) VALUES (?, ?, ?, ?, ?, '', ?, 0)",
                                (conversation_id, now, now, STATUS_ACTIVE, principal,
                                 KIND_VOICE))
                    self.conn.execute(
                        "INSERT OR REPLACE INTO conversation_sessions "
                        "(session_id, conversation_id, started_at) VALUES (?, ?, ?)",
                        (session_id, conversation_id, now))
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"begin_session failed: {exc}") from exc
        return conversation_id, resumed

    def begin_room_session(self, session_id: str, *, device_id: str,
                           owner_principal: str) -> tuple[str, bool, list[dict[str, Any]]]:
        """Publish authenticated room speech to its Owner without granting room identity.

        Only the HMAC endpoint supplies device_id and server composition supplies
        the Owner. Never accepts a conversation ID from the room/provider. Recent
        speech resumes only on the same device. App/browser clients read the
        room projection and start a separate private chat to continue privately.
        All message/session/task writers enforce that separation, including while
        room speech is active. Binding/history are one SQLite snapshot; old
        ownerless history is never reassigned. Existing room retention remains.
        """
        if not session_id or not device_id or not owner_principal:
            raise ConversationStoreError("room_binding_missing")
        now = self.now()
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute(
                        "SELECT c.conversation_id FROM conversations c "
                        "WHERE c.owner_principal=? AND c.explicit=1 AND c.status=? "
                        "AND c.room_device_id=? AND c.last_activity_at>=? "
                        "AND c.room_session_id=(SELECT s.session_id FROM conversation_sessions s "
                        " WHERE s.conversation_id=c.conversation_id ORDER BY s.started_at DESC,s.rowid DESC LIMIT 1) "
                        "AND NOT EXISTS(SELECT 1 FROM conversation_deliveries d WHERE d.conversation_id=c.conversation_id) "
                        "ORDER BY c.last_activity_at DESC,c.conversation_id LIMIT 1",
                        (owner_principal, STATUS_ACTIVE, device_id, now-self.linger_seconds)).fetchone()
                    resumed = row is not None
                    cid = row['conversation_id'] if row else 'c-' + secrets.token_hex(8)
                    if row:
                        self.conn.execute("UPDATE conversations SET last_activity_at=?,room_session_id=? WHERE conversation_id=?",
                                          (now,session_id,cid))
                    else:
                        self.conn.execute("INSERT INTO conversations (conversation_id,created_at,last_activity_at,status,"
                                          "owner_principal,title,kind,explicit,room_device_id,room_session_id) "
                                          "VALUES (?,?,?,?,?,'Gespräch am Raspberry Pi',?,1,?,?)",
                                          (cid,now,now,STATUS_ACTIVE,owner_principal,KIND_VOICE,device_id,session_id))
                    self.conn.execute("INSERT OR REPLACE INTO conversation_sessions (session_id,conversation_id,started_at) VALUES (?,?,?)",
                                      (session_id,cid,now))
                    context = self.recent_context(cid)
            except sqlite3.Error:
                raise ConversationStoreError("room_binding_unavailable") from None
        return cid, resumed, context

    def end_session(self, session_id: str, close_reason: str) -> None:
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute(
                        "UPDATE conversation_sessions SET ended_at = ?, close_reason = ? "
                        "WHERE session_id = ?", (self.now(), close_reason, session_id))
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"end_session failed: {exc}") from exc

    def create_conversation(self, *, owner_principal: str, kind: str = KIND_TEXT,
                            title: str = "", client_request_id: str) -> tuple[dict[str, Any], bool]:
        """Einen expliziten Chat anlegen — idempotent je (principal, client_request_id).

        Gibt (conversation, created) zurueck. Ein Replay findet ueber
        `conversation_creations` dieselbe Kennung; der Fremdschluessel dieser Tabelle
        sorgt dafuer, dass keine Waise eine geloeschte Kennung zurueckgibt — nach
        einer Loeschung (Hygiene, Purge, DELETE) ist auch die Anlegezeile fort, und
        dasselbe Replay legt einen NEUEN Chat an (Review Runde 18, C18-H1); der Zweig
        `conversation_deleted` unten ist nur ein Netz fuer einen Speicher ohne
        Fremdschluessel.
        """
        owner_principal = str(owner_principal or "")
        client_request_id = str(client_request_id or "")
        kind = str(kind or "")
        if not owner_principal or not client_request_id or not kind:
            raise ConversationStoreError("invalid_conversation")
        cleaned = _clean_title(title) if str(title or "").strip() else ""
        now = self.now()
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    prior = self.conn.execute(
                        "SELECT conversation_id FROM conversation_creations "
                        "WHERE principal = ? AND client_request_id = ?",
                        (owner_principal, client_request_id)).fetchone()
                    if prior is not None:
                        row = self.conn.execute(
                            "SELECT * FROM conversations WHERE conversation_id = ?",
                            (prior["conversation_id"],)).fetchone()
                        if row is None:
                            raise ConversationStoreError("conversation_deleted")
                        return dict(row), False
                    conversation_id = "c-" + secrets.token_hex(8)
                    self.conn.execute(
                        "INSERT INTO conversations (conversation_id, created_at, "
                        "last_activity_at, status, owner_principal, title, kind, explicit) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (conversation_id, now, now, STATUS_ACTIVE, owner_principal,
                         cleaned, kind))
                    self.conn.execute(
                        "INSERT INTO conversation_creations (principal, client_request_id, "
                        "conversation_id, created_at) VALUES (?, ?, ?, ?)",
                        (owner_principal, client_request_id, conversation_id, now))
                    row = self.conn.execute(
                        "SELECT * FROM conversations WHERE conversation_id = ?",
                        (conversation_id,)).fetchone()
                    return dict(row), True
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"create_conversation failed: {exc}") from exc

    def update_title(self, conversation_id: str, title: str) -> dict[str, Any]:
        """`PATCH /v1/conversations/{id}` — Titel 1..80 Zeichen, redigiert."""
        cleaned = _clean_title(title)
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    changed = self.conn.execute(
                        "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                        (cleaned, conversation_id)).rowcount
                    if changed != 1:
                        raise ConversationStoreError("unknown_conversation")
                    row = self.conn.execute(
                        "SELECT * FROM conversations WHERE conversation_id = ?",
                        (conversation_id,)).fetchone()
                    return dict(row)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"update_title failed: {exc}") from exc

    def conversation_owned(self, conversation_id: str, principal: str, *, for_write: bool = False) -> bool:
        """Aktiv, explizit, im Besitz von `principal` (nie leer). Sonst False."""
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT status, explicit, owner_principal, room_device_id FROM conversations "
                    "WHERE conversation_id = ?", (conversation_id,)).fetchone()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"conversation_owned failed: {exc}") from exc
        return _owned(row, str(principal or "")) and (not for_write or not row["room_device_id"])

    def list_conversations(self, owner_principal: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Die expliziten, aktiven Chats eines Principals — juengste Aktivitaet zuerst.

        Jede Zeile traegt `message_count`, `open_delivery_count` und
        `task_link_count`; ob ein verlinkter Auftrag noch laeuft, weiss das
        Agentenbuch, nicht dieser Speicher. Ein leerer Principal sieht nichts.
        """
        owner_principal = str(owner_principal or "")
        if not owner_principal:
            return []
        bound = max(1, min(100, int(limit or 30)))
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT c.*, "
                    "(SELECT COUNT(*) FROM conversation_messages m "
                    " WHERE m.conversation_id = c.conversation_id) AS message_count, "
                    "(SELECT COUNT(*) FROM conversation_deliveries d "
                    " WHERE d.conversation_id = c.conversation_id "
                    " AND d.status IN ('accepted','running')) AS open_delivery_count, "
                    "(SELECT COUNT(*) FROM conversation_task_links l "
                    " WHERE l.conversation_id = c.conversation_id) AS task_link_count "
                    "FROM conversations c "
                    "WHERE c.owner_principal = ? AND c.explicit = 1 AND c.status = ? "
                    "ORDER BY c.last_activity_at DESC, c.conversation_id LIMIT ?",
                    (owner_principal, STATUS_ACTIVE, bound)).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"list_conversations failed: {exc}") from exc
        return [dict(r) for r in rows]

    @contextmanager
    def _context_budget(self):
        """Bound SQLite work as well as Python scanning; caller holds _lock."""
        state = {"incomplete": False}
        steps = 0

        def progress():
            nonlocal steps
            steps += 1000
            if steps >= CONTEXT_SQL_STEPS:
                state["incomplete"] = True
                return 1
            return 0

        self.conn.set_progress_handler(progress, 1000)
        try:
            yield state
        except sqlite3.Error as exc:
            if not (state["incomplete"] and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT):
                raise ConversationStoreError(f"context search failed: {exc}") from exc
        finally:
            self.conn.set_progress_handler(None, 0)

    def _context_parameters(self, owner_principal, current_conversation_id, before_created_at, limit):
        if not self.conversation_owned(current_conversation_id, owner_principal):
            raise ConversationStoreError("unknown_conversation")
        try:
            before = float(before_created_at)
            bound = max(0, min(CONTEXT_MAX_HITS, int(limit)))
        except (ValueError, TypeError, OverflowError):
            raise ConversationStoreError("invalid_context_query") from None
        if not math.isfinite(before):
            raise ConversationStoreError("invalid_context_query")
        return min(before, self.now()), bound

    def _context_links(self, conversation_id, before, budget):
        rows = self.conn.execute(
            "SELECT task_id, run_id FROM conversation_task_links WHERE conversation_id = ? "
            "AND linked_at < ? AND task_id != '' AND run_id != '' "
            "ORDER BY linked_at DESC, task_id, revision DESC, run_id LIMIT ?",
            (conversation_id, before, CONTEXT_SCAN_ROWS + 1))
        links, seen = [], set()
        for ordinal, row in enumerate(rows):
            if ordinal >= CONTEXT_SCAN_ROWS:
                budget["incomplete"] = True
                break
            if row["task_id"] in seen:
                continue
            if len(links) == CONTEXT_LINKS_PER_HIT:
                budget["incomplete"] = True
                break
            seen.add(row["task_id"])
            links.append(dict(row))
        return links

    def search_context(self, owner_principal: str, query: str, *, current_conversation_id: str,
                       before_created_at: float, limit: int = 5, max_chars: int = 1800) -> dict:
        """Owned earlier chat excerpts, ranked lexically before the result cap.

        Reuses memory's content words, never its providers. Phrase/title hits
        rank ahead of overlapping words. SQL receives no search expression;
        punctuation is literal. Exhausted scan/output budgets are explicit.
        One best matching message per chat, before applying the result cap.
        max_chars bounds all excerpts together; a shortened excerpt does not
        make the candidate search incomplete. Titles/links have separate caps.
        Task links are chat-owned references: the caller must recheck run/task
        existence and ownership in the canonical agent ledger before using them.
        """
        from solvio.memory.service import _tokens

        hits, candidates = [], []
        with self._lock:
            before, bound = self._context_parameters(owner_principal, current_conversation_id, before_created_at, limit)
            try:
                room = max(0, min(6000, int(max_chars)))
            except (ValueError, TypeError, OverflowError):
                raise ConversationStoreError("invalid_context_query") from None
            if not isinstance(query, str):
                raise ConversationStoreError("invalid_context_query")
            if not query.strip():
                return {"hits": [], "incomplete": False}
            if not bound or not room or len(query) > CONTEXT_QUERY_CHARS:
                return {"hits": [], "incomplete": True}
            needle = _context_fold(query.strip())
            tokens = _tokens(needle)
            with self._context_budget() as budget:
                rows = self.conn.execute(
                    "SELECT c.conversation_id, c.title, COALESCE(m.message_id, '') AS message_id, "
                    "COALESCE(m.sequence, 0) AS sequence, COALESCE(m.role, '') AS role, "
                    "COALESCE(m.created_at, c.created_at) AS created_at, "
                    "substr(COALESCE(m.text, ''), 1, ?) AS text FROM conversations c "
                    "LEFT JOIN conversation_messages m ON m.conversation_id = c.conversation_id "
                    "AND m.created_at < ? AND m.role IN ('user','assistant') "
                    "WHERE c.owner_principal = ? AND c.explicit = 1 AND c.status = ? "
                    "AND c.conversation_id != ? AND c.created_at < ? "
                    "ORDER BY c.last_activity_at DESC, c.conversation_id, m.sequence DESC",
                    (CONTEXT_MESSAGE_CHARS + 1, before, owner_principal, STATUS_ACTIVE,
                     current_conversation_id, before))
                scanned, processed, per_chat, trimmed, matched = 0, 0, {}, set(), set()
                for row in rows:
                    if processed >= CONTEXT_SCAN_ROWS or scanned > CONTEXT_SCAN_CHARS:
                        budget["incomplete"] = True
                        break
                    # Das Zeilenbudget gilt zusaetzlich JE GESPRAECH: ein gespraechiger
                    # neuer Chat draengt sonst aeltere aus der Reichweite, und die
                    # Fortsetzung ueber Chatgrenzen verfaellt still mit wachsender
                    # Historie (DEBT-0309, Review Runde 22, Verifikation N-3).
                    # Die Zeilen eines Gespraechs kommen nach `sequence DESC`, die
                    # Kappe behaelt also die juengsten Nachrichten je Chat.
                    chat_rows = per_chat.get(row["conversation_id"], 0)
                    if chat_rows >= CONTEXT_SCAN_ROWS_PER_CHAT:
                        trimmed.add(row["conversation_id"])
                        continue
                    per_chat[row["conversation_id"]] = chat_rows + 1
                    processed += 1
                    scanned += len(row["text"]) + len(row["title"])
                    text = row["text"][:CONTEXT_MESSAGE_CHARS]
                    if len(row["text"]) > CONTEXT_MESSAGE_CHARS:
                        budget["incomplete"] = True
                    title, folded = _context_fold(row["title"]), _context_fold(text)
                    title_words, words = tokens & _tokens(title), tokens & _tokens(folded)
                    score = (int(needle in title), int(needle in folded), len(title_words), len(words))
                    if any(score):
                        matched.add(row["conversation_id"])
                        candidates.append((score, dict(row), words))
                # Unvollstaendig heisst: ein KANDIDAT koennte fehlen — `incomplete` zwingt den
                # Chatweg bei jeder Fortsetzung aus einem anderen Chat in eine Rueckfrage. Ein
                # gekuerztes Gespraech, das in seinen juengsten Zeilen ohnehin passt, ist
                # vertreten (Review Runde 23, K23B-2). Und ein gekuerztes Gespraech ohne jede
                # Auftragsverknuepfung kann gar keinen Kandidaten verbergen: Kandidaten entstehen
                # nur aus verknuepften Auftraegen. Ohne diese zweite Ausnahme loeste schon ein
                # langer Plauder- oder Sprachchat ohne Auftrag bei JEDER Fortsetzung eine
                # Rueckfrage aus (Review Runde 24, R24-1, gemessen ab 257 Nachrichten).
                hidden = sorted(trimmed - matched)
                if hidden:
                    marks = ",".join("?" * len(hidden))
                    linked = self.conn.execute(
                        "SELECT 1 FROM conversation_task_links WHERE conversation_id IN (" + marks + ") "
                        "AND linked_at < ? LIMIT 1", (*hidden, before)).fetchone()
                    if linked is not None:
                        budget["incomplete"] = True
                candidates.sort(key=lambda item: (
                    tuple(-part for part in item[0]), -item[1]["created_at"],
                    item[1]["conversation_id"], -item[1]["sequence"]))
                # Many matching messages in one chat are one topic candidate,
                # not a reason to push other conversations past the limit.
                by_chat = {}
                for candidate in candidates:
                    by_chat.setdefault(candidate[1]["conversation_id"], candidate)
                candidates = list(by_chat.values())
                if len(candidates) > bound:
                    budget["incomplete"] = True
                for _, row, common in candidates[:bound]:
                    if not room:
                        budget["incomplete"] = True
                        break
                    excerpt = _context_excerpt(row["text"][:CONTEXT_MESSAGE_CHARS], needle, common,
                                               min(CONTEXT_EXCERPT_CHARS, room))
                    links = self._context_links(row["conversation_id"], before, budget)
                    hits.append({key: row[key] for key in ("conversation_id", "title", "message_id", "sequence", "role")} |
                                {"excerpt": excerpt, "task_links": links, "match_kind": "topic"})
                    room -= len(excerpt)
            return {"hits": hits, "incomplete": budget["incomplete"]}

    def recent_task_links(self, owner_principal: str, *, current_conversation_id: str,
                          before_created_at: float, limit: int = 6) -> dict:
        """Recent links from owned chats; no topic claim and no task authority.

        "Recent" is a statement about time: links older than
        RECENT_LINK_MAX_AGE_SECONDS never enter this fallback, however new they
        are relative to the others (DEBT-0308). A topic hit may be older — it
        carries evidence; this list does not.

        One most recent link per task before the result cap: its older
        revisions cannot hide other tasks. A chat may carry a task card without a message: its message_id/role are
        then empty and sequence is zero. Future messages/links never enter the
        result. Agent-ledger validity is the caller's separate read-time check.
        """
        hits, seen = [], set()
        with self._lock:
            before, bound = self._context_parameters(owner_principal, current_conversation_id, before_created_at, limit)
            if not bound:
                return {"hits": [], "incomplete": True}
            with self._context_budget() as budget:
                rows = self.conn.execute(
                    "SELECT c.conversation_id, c.title, l.task_id, l.run_id, "
                    "COALESCE(m.message_id, '') AS message_id, COALESCE(m.sequence, 0) AS sequence, "
                    "COALESCE(m.role, '') AS role, substr(COALESCE(m.text, ''), 1, ?) AS text "
                    "FROM conversations c JOIN conversation_task_links l ON l.conversation_id = c.conversation_id "
                    "LEFT JOIN conversation_messages m ON m.message_id = (SELECT prior.message_id "
                    "FROM conversation_messages prior WHERE prior.conversation_id = c.conversation_id "
                    "AND prior.created_at < ? AND prior.role IN ('user','assistant') ORDER BY prior.sequence DESC LIMIT 1) "
                    "WHERE c.owner_principal = ? AND c.explicit = 1 AND c.status = ? "
                    "AND c.conversation_id != ? AND c.created_at < ? AND l.linked_at < ? "
                    "AND l.linked_at >= ? "
                    "AND l.task_id != '' AND l.run_id != '' "
                    "ORDER BY l.linked_at DESC, c.conversation_id, l.task_id, l.revision DESC, l.run_id LIMIT ?",
                    (CONTEXT_EXCERPT_CHARS + 1, before, owner_principal, STATUS_ACTIVE,
                     current_conversation_id, before, before,
                     before - RECENT_LINK_MAX_AGE_SECONDS, CONTEXT_SCAN_ROWS + 1))
                for ordinal, row in enumerate(rows):
                    if ordinal >= CONTEXT_SCAN_ROWS:
                        budget["incomplete"] = True
                        break
                    if row["task_id"] in seen:
                        continue
                    if len(hits) == bound:
                        budget["incomplete"] = True
                        break
                    seen.add(row["task_id"])
                    hits.append({key: row[key] for key in ("conversation_id", "title", "message_id", "sequence", "role")} |
                                {"excerpt": row["text"][:CONTEXT_EXCERPT_CHARS], "task_links": [{"task_id": row["task_id"], "run_id": row["run_id"]}],
                                 "match_kind": "recent"})
            return {"hits": hits, "incomplete": budget["incomplete"]}

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
        if not (text or "").strip():
            return ""
        with self._lock:
            now = self.now()
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    message_id, _ = self._insert_message(
                        self.conn, conversation_id, role, text,
                        source_session_id=source_session_id,
                        source_turn_id=source_turn_id, message_id=message_id, now=now)
                    self._touch_activity(self.conn, conversation_id, now)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"add_message failed: {exc}") from exc
        return message_id

    def append_mail_outcome(self, entry: dict, summary: str) -> bool:
        """Idempotent presentation of a Core-owned pending-start result.

        Waiting for the original delivery's final message preserves chronology.
        Removed conversations stay removed; no result can recreate one.
        """
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute("SELECT * FROM conversation_deliveries WHERE delivery_id=? "
                        "AND conversation_id=? AND principal=?", (entry['delivery_ref'],
                        entry['conversation_ref'], entry['principal'])).fetchone()
                    if row is None:
                        return True
                    if row['status'] in DELIVERY_OPEN:
                        return False
                    import hashlib
                    message_id = 'm-' + hashlib.sha256(('mail-result:' + entry['request_id']).encode()).hexdigest()[:16]
                    if self.conn.execute("SELECT 1 FROM conversation_messages WHERE message_id=?", (message_id,)).fetchone():
                        return True
                    self._insert_message(self.conn, entry['conversation_ref'], ROLE_ASSISTANT, summary,
                        source_session_id=None, source_turn_id=entry['delivery_ref'], message_id=message_id)
                    self._touch_activity(self.conn, entry['conversation_ref'], self.now())
                    return True
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"append_mail_outcome failed: {exc}") from exc

    def recent_context(self, conversation_id: str, *,
                       max_chars: int = DEFAULT_CONTEXT_CHARS,
                       before_sequence: int | None = None) -> list[dict[str, Any]]:
        """Das juengste Fenster des Gespraechs, in chronologischer Reihenfolge.

        Deterministisch und woertlich: keine Zusammenfassung, kein zweites Modell. Passt
        eine Nachricht nicht mehr ins Budget, faellt sie GANZ weg — es wird nicht mitten in
        einem Satz oder gar in einem UTF-8-Zeichen geschnitten. Aeltestes faellt zuerst.

        `before_sequence` (C3, exklusiv): nur Nachrichten mit `sequence < ?` — der
        eigene Text einer Zustellung geht als `user_text` getrennt hinein. `None` ist
        das Verhalten von vorher, bytegleich.
        """
        with self._lock:
            try:
                if before_sequence is None:
                    rows = self.conn.execute(
                        "SELECT role, text, sequence FROM conversation_messages "
                        "WHERE conversation_id = ? ORDER BY sequence DESC",
                        (conversation_id,)).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT role, text, sequence FROM conversation_messages "
                        "WHERE conversation_id = ? AND sequence < ? ORDER BY sequence DESC",
                        (conversation_id, int(before_sequence))).fetchall()
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
        """One already persisted message, bound to its original conversation.

        Every reader of the processing path raises ConversationStoreError, never raw
        sqlite3 (review round 12, C12-H2 after K10-4): a raw error fell into the
        processor's catch-all and blocked the delivery TERMINALLY, while the same
        cause behind a wrapped reader leaves it for recovery.
        """
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT message_id, role, text, source_session_id, source_turn_id "
                    "FROM conversation_messages WHERE conversation_id=? AND message_id=?",
                    (conversation_id, message_id)).fetchone()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"message failed: {exc}") from exc
        return dict(row) if row else None

    def turn_user_text(self, conversation_id: str, turn_id: str, *,
                       upto_sequence: int | None = None) -> str:
        """Alle NUTZER-Zeilen eines Turns dieses Gespraechs, in Reihenfolge — ohne Fenster.

        Ein Glied der Rueckfragekette darf nicht daran scheitern, dass es
        aelter als die juengsten 40 Zeilen ist (Review Runde 4, R4-F-1: ein
        vorhandener Sprach-Turn galt als fehlend, jede weitere Auftragsnachricht
        scheiterte). Gebunden an das Gespraech, an `role='user'` und an die
        Sequenz der lesenden Zustellung.
        """
        turn_id = str(turn_id or "")
        if not turn_id:
            return ""
        where, params = ["conversation_id = ?", "source_turn_id = ?", "role = ?"], [conversation_id, turn_id, ROLE_USER]
        if upto_sequence is not None:
            where.append("sequence <= ?"); params.append(int(upto_sequence))
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT text FROM conversation_messages WHERE " + " AND ".join(where) + " ORDER BY sequence",
                    params).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"turn_user_text failed: {exc}") from exc
        return " ".join(part for part in (str(r["text"] or "").strip() for r in rows) if part)

    def messages(self, conversation_id: str, *, limit: int = 0,
                 upto_sequence: int | None = None,
                 after_sequence: int | None = None) -> list[dict[str, Any]]:
        """Die Nachrichten einer Konversation, chronologisch.

        `limit > 0` liest nur die juengsten und gibt sie trotzdem chronologisch
        zurueck. Der Grund ist der Sprachweg: seit der Router den vorigen
        Nutzer-Turn bindet, laeuft dieser Leseweg in JEDEM Turn statt nur nach
        einer Rueckfrage. Ein voller Tabellenlauf pro gesprochenem Satz waere
        eine Latenzschuld, die mit dem Gespraech waechst.

        `upto_sequence` (C3, inklusiv): nur `sequence <= ?` — die eigene Nachricht einer
        Zustellung bleibt sichtbar und wird wie bisher ueber `source_turn_id`
        uebersprungen; was DANACH angenommen wurde, ist fuer diese Verarbeitung
        unsichtbar. `after_sequence` (exklusiv): nur `sequence > ?`, fuer das
        Nachlesen im Endpunkt. `None` ist jeweils das Verhalten von vorher.
        """
        conditions = ["conversation_id = ?"]
        params: list[Any] = [conversation_id]
        if upto_sequence is not None:
            conditions.append("sequence <= ?")
            params.append(int(upto_sequence))
        if after_sequence is not None:
            conditions.append("sequence > ?")
            params.append(int(after_sequence))
        where = " AND ".join(conditions)
        columns = ("message_id, sequence, role, text, created_at, source_session_id, "
                   "source_turn_id")
        with self._lock:
            try:
                if int(limit) > 0:
                    rows = self.conn.execute(
                        f"SELECT {columns} FROM conversation_messages WHERE {where} "
                        "ORDER BY sequence DESC LIMIT ?", (*params, int(limit))).fetchall()
                    return [dict(r) for r in reversed(rows)]
                rows = self.conn.execute(
                    f"SELECT {columns} FROM conversation_messages WHERE {where} "
                    "ORDER BY sequence", params).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"messages failed: {exc}") from exc
        return [dict(r) for r in rows]

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (conversation_id,)).fetchone()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"conversation failed: {exc}") from exc
        return dict(row) if row else None

    def sessions_of(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT * FROM conversation_sessions WHERE conversation_id = ? ORDER BY started_at",
                    (conversation_id,)).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"sessions_of failed: {exc}") from exc
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- Zustellungen (C3)
    def bind_voice_mail(self, *, conversation_id: str, principal: str, message_id: str,
                        session_id: str, text: str, device_id: str, core_id: str,
                        source_generation: str, call: dict) -> tuple[dict[str, Any], bool]:
        """Journal one Core-selected mail against its existing spoken original.

        Authentication is checked by voice_scope before and throughout effects.
        This transaction verifies ownership and the exact stored original and
        claims it once. It is a completed selection, not a new processor job:
        the existing pending-start/Face-ID tick alone owns later sending.
        """
        import hashlib
        dispatch = json.dumps({'action_class':'mail', 'objective':text,
                               'mail_call':call, 'mail_claimed':True},
                              sort_keys=True, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(dispatch.encode('utf-8')).hexdigest()
        client_id = 'voice-mail:' + message_id
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute('BEGIN IMMEDIATE')
                    conversation = self.conn.execute('SELECT * FROM conversations WHERE conversation_id=?',
                                                     (conversation_id,)).fetchone()
                    original = self.conn.execute('SELECT * FROM conversation_messages WHERE conversation_id=? '
                        'AND message_id=? AND role=? AND source_session_id=? AND text=?',
                        (conversation_id, message_id, ROLE_USER, session_id, text.strip())).fetchone()
                    if (not _owned(conversation, principal) or conversation['room_device_id'] or original is None
                            or not device_id or not core_id or not source_generation):
                        raise ConversationStoreError('invalid_voice_mail_source')
                    prior = self.conn.execute('SELECT * FROM conversation_deliveries WHERE conversation_id=? '
                        'AND client_message_id=?', (conversation_id, client_id)).fetchone()
                    if prior is not None:
                        if prior['digest'] != digest:
                            raise ConversationStoreError('message_conflict')
                        return dict(prior), False
                    delivery_id = 'cd-' + secrets.token_hex(8)
                    now = self.now()
                    self.conn.execute('INSERT INTO conversation_deliveries (delivery_id, conversation_id, '
                        'client_message_id, principal, source_kind, source_ref, device_id, core_id, source_generation, '
                        'digest, status, worker_generation, message_id, message_sequence, dispatch, created_at, updated_at) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (delivery_id, conversation_id, client_id, principal, 'app', 'voice:'+session_id,
                         device_id, core_id, source_generation, digest, DELIVERY_COMPLETED, '', message_id,
                         original['sequence'], dispatch, now, now))
                    row = self.conn.execute('SELECT * FROM conversation_deliveries WHERE delivery_id=?',
                                            (delivery_id,)).fetchone()
                    return dict(row), True
            except sqlite3.Error as exc:
                raise ConversationStoreError(f'bind_voice_mail failed: {exc}') from exc

    def accept_delivery(self, *, conversation_id: str, principal: str, client_message_id: str,
                        text: str, digest: str, source_kind: str, source_ref: str,
                        core_id: str, source_generation: str, device_id: str = "",
                        attachments: str = "", target: str = "") -> tuple[dict[str, Any], bool]:
        """Annahme einer Textnachricht — genau EINE Transaktion (§2.3).

        1. Eigentum (aktiv, explizit, `owner_principal=principal`) in derselben
           Transaktion, vor allem anderen — auch vor dem Replay (Review Runde 2,
           C2-5); sonst `unknown_conversation`.
        2. Replay: gleiche `client_message_id` mit gleichem `digest` → vorhandene Zeile,
           `created=False`, kein weiterer Schreibvorgang; anderer Digest →
           `message_conflict`.
        3. Nutzernachricht (`source_turn_id=delivery_id`), Titel falls leer,
           Aktivitaet.
        4. Zustellzeile `accepted` mit `message_sequence` — die Obergrenze fuer jeden
           Kontextleser dieser Zustellung.

        Der Verarbeitungstext ist der persistierte, redigierte Nachrichtentext — nie
        der Request-Body. Gibt (delivery, created) zurueck.
        """
        if source_kind not in DELIVERY_SOURCE_KINDS:
            raise ConversationStoreError("invalid_delivery_source")
        for name, value in (("client_message_id", client_message_id), ("digest", digest),
                            ("source_ref", source_ref), ("core_id", core_id),
                            ("source_generation", source_generation), ("principal", principal)):
            if not isinstance(value, str) or not value:
                raise ConversationStoreError(f"invalid_delivery:{name}")
        if not (text or "").strip():
            raise ConversationStoreError("empty_message")
        with self._lock:
            now = self.now()
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    # Eigentum VOR dem Replay: sonst bekaeme ein fremder Principal, der
                    # Chat-, Nachrichtenkennung und Digest kennt, die Zustellkennungen
                    # zurueck (Review Runde 2, C2-5).
                    conversation = self.conn.execute(
                        "SELECT * FROM conversations WHERE conversation_id = ?",
                        (conversation_id,)).fetchone()
                    if not _owned(conversation, principal):
                        raise ConversationStoreError("unknown_conversation")
                    if conversation["room_device_id"]:
                        raise ConversationStoreError("room_conversation_read_only")
                    prior = self.conn.execute(
                        "SELECT * FROM conversation_deliveries "
                        "WHERE conversation_id = ? AND client_message_id = ?",
                        (conversation_id, client_message_id)).fetchone()
                    if prior is not None:
                        if prior["digest"] != digest:
                            raise ConversationStoreError("message_conflict")
                        return dict(prior), False
                    delivery_id = "cd-" + secrets.token_hex(8)
                    message_id, sequence = self._insert_message(
                        self.conn, conversation_id, ROLE_USER, text,
                        source_session_id=None, source_turn_id=delivery_id,
                        message_id=None, now=now)
                    if not conversation["title"]:
                        persisted = self.conn.execute(
                            "SELECT text FROM conversation_messages WHERE message_id = ?",
                            (message_id,)).fetchone()["text"]
                        # Der Tresor-Platzhalter ist kein Titel (Review Runde 4, B-H2).
                        if persisted != TRANSCRIPT_MARKER:
                            self.conn.execute(
                                "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                                (derive_title(persisted), conversation_id))
                    self._touch_activity(self.conn, conversation_id, now)
                    self.conn.execute(
                        "INSERT INTO conversation_deliveries (delivery_id, conversation_id, "
                        "client_message_id, principal, source_kind, source_ref, device_id, "
                        "core_id, source_generation, digest, status, worker_generation, "
                        "message_id, message_sequence, attachments, target, created_at, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
                        (delivery_id, conversation_id, client_message_id, principal,
                         source_kind, source_ref, str(device_id or ""), core_id,
                         source_generation, digest, DELIVERY_ACCEPTED, message_id, sequence,
                         str(attachments or ""), str(target or ""), now, now))
                    row = self.conn.execute(
                        "SELECT * FROM conversation_deliveries WHERE delivery_id = ?",
                        (delivery_id,)).fetchone()
                    return dict(row), True
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"accept_delivery failed: {exc}") from exc

    def claim_next_delivery(self, conversation_id: str,
                            worker_generation: str) -> dict[str, Any] | None:
        """Bedingte Uebernahme `accepted → running` mit Prozessgeneration (§3.0).

        Die aelteste Zeile des Chats, die `accepted` ist oder `running` unter einer
        FREMDEN Generation (Neustart-Waise), wird mit einem bedingten `UPDATE`
        uebernommen. Eine `running`-Zeile derselben Generation gehoert einem Worker
        dieses Prozesses und wird nie uebernommen. `rowcount != 1` → None.

        Reihenfolge: `created_at`, bei gleicher Zeit die Annahmereihenfolge
        (`message_sequence` — je Chat lueckenlos steigend), zuletzt `delivery_id`.
        """
        worker_generation = str(worker_generation or "")
        if not worker_generation:
            raise ConversationStoreError("worker_generation_required")
        with self._lock:
            now = self.now()
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute(
                        "SELECT delivery_id FROM conversation_deliveries "
                        "WHERE conversation_id = ? AND (status = 'accepted' "
                        "OR (status = 'running' AND worker_generation <> ?)) "
                        "ORDER BY created_at, message_sequence, delivery_id LIMIT 1",
                        (conversation_id, worker_generation)).fetchone()
                    if row is None:
                        return None
                    claimed = self.conn.execute(
                        "UPDATE conversation_deliveries SET status = 'running', "
                        "worker_generation = ?, updated_at = ? WHERE delivery_id = ? "
                        "AND status IN ('accepted','running') AND worker_generation <> ?",
                        (worker_generation, now, row["delivery_id"], worker_generation)).rowcount
                    if claimed != 1:
                        return None
                    claimed_row = self.conn.execute(
                        "SELECT * FROM conversation_deliveries WHERE delivery_id = ?",
                        (row["delivery_id"],)).fetchone()
                    return dict(claimed_row)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"claim_next_delivery failed: {exc}") from exc

    def _guarded_update(self, assignments: str, params: tuple, delivery_id: str,
                        worker_generation: str) -> None:
        """Ein Schreibvorgang auf eine Zustellung, die DIESER Worker haelt.

        `WHERE delivery_id=? AND status='running' AND worker_generation=?` —
        `rowcount != 1` heisst: die Zeile gehoert nicht mehr diesem Worker
        (Endzustand, fremde Uebernahme, geloescht) → `delivery_lost`, und die
        umgebende Transaktion rollt zurueck.
        """
        changed = self.conn.execute(
            f"UPDATE conversation_deliveries SET {assignments} WHERE delivery_id = ? "
            "AND status = 'running' AND worker_generation = ?",
            (*params, delivery_id, worker_generation)).rowcount
        if changed != 1:
            raise ConversationStoreError("delivery_lost")

    def record_dispatch(self, delivery_id: str, worker_generation: str,
                        dispatch_json: str) -> None:
        """Den Dispatch-Entscheid persistieren — VOR jeder Wirkung (§3.3)."""
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self._guarded_update("dispatch = ?, updated_at = ?",
                                         (str(dispatch_json or ""), self.now()),
                                         delivery_id, str(worker_generation or ""))
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"record_dispatch failed: {exc}") from exc

    def record_activity(self, delivery_id: str, worker_generation: str,
                        activity_id: str) -> None:
        """Die Aktivitaetskennung nach dem ersten `admit` festhalten (§3.2)."""
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self._guarded_update("activity_id = ?, updated_at = ?",
                                         (str(activity_id or ""), self.now()),
                                         delivery_id, str(worker_generation or ""))
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"record_activity failed: {exc}") from exc

    def complete_delivery(self, delivery_id: str, worker_generation: str, *,
                          assistant_text: str = "", task_id: str = "", run_id: str = "",
                          revision: int = 0, learning_pending: bool = False) -> dict[str, Any]:
        """Abschluss einer Zustellung — eine Transaktion (§3.4–§3.6).

        Assistentennachricht (`source_turn_id=delivery_id`), Link
        `conversation_task_links(..., source=delivery_id)` bei `task_id`, Zeile
        `completed` mit `assistant_message_id`/`task_id`/`run_id`/`revision`. Jeder
        Schritt haengt am Generationsguard: `delivery_lost` rollt alles zurueck.
        """
        task_id, run_id = str(task_id or ""), str(run_id or "")
        if bool(task_id) != bool(run_id):
            raise ConversationStoreError("invalid_task_link")
        worker_generation = str(worker_generation or "")
        with self._lock:
            now = self.now()
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute(
                        "SELECT * FROM conversation_deliveries WHERE delivery_id = ?",
                        (delivery_id,)).fetchone()
                    if (row is None or row["status"] != DELIVERY_RUNNING
                            or row["worker_generation"] != worker_generation):
                        raise ConversationStoreError("delivery_lost")
                    conversation_id = row["conversation_id"]
                    assistant_message_id = ""
                    if (assistant_text or "").strip():
                        assistant_message_id, _ = self._insert_message(
                            self.conn, conversation_id, ROLE_ASSISTANT, assistant_text,
                            source_session_id=None, source_turn_id=delivery_id,
                            message_id=None, now=now)
                    if task_id:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO conversation_task_links (conversation_id, "
                            "task_id, run_id, revision, linked_at, source) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (conversation_id, task_id, run_id, int(revision or 1), now,
                             delivery_id))
                    dispatch = row["dispatch"]
                    if learning_pending:
                        decision = json.loads(dispatch or "{}")
                        if (task_id or row["attachments"] or not assistant_message_id
                                or decision.get("action_class") != "frage"):
                            raise ConversationStoreError("invalid_learning_delivery")
                        decision["learning"] = "pending"
                        dispatch = json.dumps(decision, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                    self._guarded_update(
                        "status = 'completed', assistant_message_id = ?, task_id = ?, "
                        "run_id = ?, revision = ?, updated_at = ?, dispatch = ?",
                        (assistant_message_id, task_id, run_id, int(revision or 0), now, dispatch),
                        delivery_id, worker_generation)
                    self._touch_activity(self.conn, conversation_id, now)
                    final = self.conn.execute(
                        "SELECT * FROM conversation_deliveries WHERE delivery_id = ?",
                        (delivery_id,)).fetchone()
                    return dict(final)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"complete_delivery failed: {exc}") from exc

    def pending_learning(self, *, after_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Page the existing completion markers, without replaying a delivery."""
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT * FROM conversation_deliveries WHERE status='completed' AND delivery_id>? "
                    "AND instr(dispatch, '\"learning\":\"pending\"')>0 ORDER BY delivery_id LIMIT ?",
                    (after_id, min(100, max(1, int(limit))))).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"pending_learning failed: {exc}") from exc

    def learning_delivery(self, principal: str, conversation_id: str, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT * FROM conversation_deliveries WHERE principal=? AND conversation_id=? "
                    "AND message_id=? AND status='completed'", (principal, conversation_id, message_id)).fetchone()
                return dict(row) if row else None
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"learning_delivery failed: {exc}") from exc

    def learning_offered(self, delivery_id: str) -> None:
        """Called only after durable activity admission; the activity owns further recovery."""
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute("SELECT dispatch FROM conversation_deliveries "
                        "WHERE delivery_id=? AND status='completed'", (delivery_id,)).fetchone()
                    if row is None:
                        return
                    dispatch = json.loads(row['dispatch'] or '{}')
                    if dispatch.get('learning') != 'pending':
                        return
                    dispatch['learning'] = 'offered'
                    self.conn.execute("UPDATE conversation_deliveries SET dispatch=?,updated_at=? WHERE delivery_id=?",
                        (json.dumps(dispatch, sort_keys=True, ensure_ascii=False, separators=(',', ':')),
                         self.now(), delivery_id))
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"learning_offered failed: {exc}") from exc

    def block_delivery(self, delivery_id: str, worker_generation: str, *,
                       error_code: str) -> dict[str, Any]:
        """Endzustand `blocked` mit `error_code` — nur unter derselben Generation."""
        error_code = str(error_code or "")
        if not error_code:
            raise ConversationStoreError("error_code_required")
        with self._lock:
            now = self.now()
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self._guarded_update("status = 'blocked', error_code = ?, updated_at = ?",
                                         (error_code, now), delivery_id,
                                         str(worker_generation or ""))
                    final = self.conn.execute(
                        "SELECT * FROM conversation_deliveries WHERE delivery_id = ?",
                        (delivery_id,)).fetchone()
                    self._touch_activity(self.conn, final["conversation_id"], now)
                    return dict(final)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"block_delivery failed: {exc}") from exc

    def delivery(self, conversation_id: str, delivery_id: str) -> dict[str, Any] | None:
        """Eine Zustellung — gebunden an ihr Gespraech."""
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT * FROM conversation_deliveries WHERE conversation_id = ? "
                    "AND delivery_id = ?", (conversation_id, delivery_id)).fetchone()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"delivery failed: {exc}") from exc
        return dict(row) if row else None

    def deliveries(self, conversation_id: str) -> list[dict[str, Any]]:
        """Alle Zustellungen eines Gespraechs, in Annahme-Reihenfolge — OHNE Anhangsbytes.

        Die Detailsicht liest diese Liste bei jedem Poll unter dem Store-Lock;
        `attachments` (bis ~11 MB JSON je Zustellung) braucht nur die Verarbeitung
        einer einzelnen Zustellung (`delivery()`), nie eine Sicht (Review Runde 3,
        C3-3). Wer die Bytes braucht, liest die eine Zeile.
        """
        # Die Leseseite der Wiederaufnahme wirft ConversationStoreError, nie rohes
        # sqlite3 (Review Runde 10, K10-4: ein roher Fehler beim Wecken nach dem
        # Neustart liess die Wiederaufnahme ungeloggt sterben).
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT delivery_id, conversation_id, client_message_id, principal, source_kind, "
                    "source_ref, device_id, core_id, source_generation, digest, status, worker_generation, "
                    "message_id, message_sequence, target, dispatch, assistant_message_id, task_id, run_id, "
                    "revision, error_code, activity_id, created_at, updated_at "
                    "FROM conversation_deliveries WHERE conversation_id = ? "
                    "ORDER BY created_at, message_sequence, delivery_id", (conversation_id,)).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"deliveries failed: {exc}") from exc
        return [dict(r) for r in rows]

    def open_delivery_count(self, conversation_id: str) -> int:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM conversation_deliveries WHERE conversation_id = ? "
                    "AND status IN ('accepted','running')", (conversation_id,)).fetchone()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"open_delivery_count failed: {exc}") from exc
        return int(row["n"])

    def conversations_with_open_deliveries(self) -> list[str]:
        """Die Chats mit `accepted`/`running`-Zustellungen — fuer `recover()` beim Start."""
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT conversation_id, MIN(created_at) AS oldest FROM conversation_deliveries "
                    "WHERE status IN ('accepted','running') GROUP BY conversation_id "
                    "ORDER BY oldest, conversation_id").fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"conversations_with_open_deliveries failed: {exc}") from exc
        return [r["conversation_id"] for r in rows]

    # ----------------------------------------------------------------- Auftragsverweise (C3)
    def add_task_link(self, conversation_id: str, task_id: str, run_id: str, *,
                      revision: int = 1, source: str) -> bool:
        """Einen Auftrag an einen Chat binden — idempotent je (chat, task, run).

        `source` ist `delivery_id`, `'voice:<session_id>'` oder `'task:<request_id>'`.
        Gibt True zurueck, wenn die Zeile neu war.
        """
        task_id, run_id, source = str(task_id or ""), str(run_id or ""), str(source or "")
        if not task_id or not run_id or not source:
            raise ConversationStoreError("invalid_task_link")
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self._require_room_source(self.conn, conversation_id,
                        source[len("voice:"):] if source.startswith("voice:") else None)
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO conversation_task_links (conversation_id, "
                        "task_id, run_id, revision, linked_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                        (conversation_id, task_id, run_id, int(revision or 1), self.now(),
                         source)).rowcount
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"add_task_link failed: {exc}") from exc
        return inserted == 1

    def task_links(self, conversation_id: str) -> list[dict[str, Any]]:
        """Die Auftragsverweise DIESES Chats — und nur dieses."""
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT * FROM conversation_task_links WHERE conversation_id = ? "
                    "ORDER BY linked_at, task_id, run_id", (conversation_id,)).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"task_links failed: {exc}") from exc
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- Loeschen
    def delete_conversation(self, conversation_id: str) -> int:
        """Ein Gespraech restlos entfernen. Deterministisch, ohne Zeitplan und ohne UI —
        die Aufbewahrungsentscheidung faellt spaeter, das Primitiv soll dann dastehen.

        C3: solange eine Zustellung `accepted`/`running` ist → `deliveries_open`
        (geprueft INNERHALB derselben Transaktion). Auftraege im Agentenbuch bleiben.
        """
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    open_rows = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM conversation_deliveries "
                        "WHERE conversation_id = ? AND status IN ('accepted','running')",
                        (conversation_id,)).fetchone()["n"]
                    if int(open_rows) > 0:
                        raise ConversationStoreError("deliveries_open")
                    n = self._delete_conversation_rows(self.conn, conversation_id)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"delete_conversation failed: {exc}") from exc
        return n

    def purge_expired(self, *, now: float | None = None) -> dict:
        """Woertlichen Gespraechsverlauf entfernen, der aelter als die Aufbewahrungsfrist ist.

        Entfernt werden GESPRAECHE samt ihren Nachrichten und Sitzungszeilen — nicht
        einzelne Nachrichten aus einem noch laufenden Gespraech, denn ein halb
        entkerntes Gespraech waere schlimmer als ein ganzes oder gar keines.

        C3: private explizite Chats sind von der Aufbewahrungsregel ausgenommen.
        Sichtbare Raumgespraeche behalten die bestehende Sprach-Aufbewahrungsfrist. Im selben Lauf faellt die 24-h-Hygiene leerer expliziter
        Chats (keine Nachricht, keine Zustellung, kein Auftragsverweis, aelter als 24 h) — eigener Zaehler
        `empty_explicit`. Beide Wege loeschen ueber `_delete_conversation_rows`.

        Dauerhaftes Gedaechtnis liegt in einer anderen Datenbank und wird hiervon NICHT
        beruehrt. Es gibt bewusst keine Kaskade.
        """
        moment = self.now() if now is None else now
        cutoff = moment - self.retention_days * 86400.0
        hygiene_cutoff = moment - EMPTY_EXPLICIT_HYGIENE_SECONDS
        with self._lock:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    rows = self.conn.execute(
                        "SELECT conversation_id FROM conversations "
                        "WHERE last_activity_at < ? AND (explicit = 0 OR room_device_id != '')", (cutoff,)).fetchall()
                    ids = [r["conversation_id"] for r in rows]
                    messages = 0
                    for conversation_id in ids:
                        messages += self._delete_conversation_rows(self.conn, conversation_id)
                    # Leer heisst: keine Nachricht, keine Zustellung UND kein Auftragsverweis —
                    # ein Chat, an den ein Auftrag ueber /v1/agent/tasks gebunden wurde, traegt
                    # nur einen Link und ist kein Waisenchat (Review Runde 3, C3-2).
                    empty = self.conn.execute(
                        "SELECT c.conversation_id FROM conversations c "
                        "WHERE c.explicit = 1 AND c.created_at < ? "
                        "AND NOT EXISTS (SELECT 1 FROM conversation_messages m "
                        "                WHERE m.conversation_id = c.conversation_id) "
                        "AND NOT EXISTS (SELECT 1 FROM conversation_deliveries d "
                        "                WHERE d.conversation_id = c.conversation_id) "
                        "AND NOT EXISTS (SELECT 1 FROM conversation_task_links l "
                        "                WHERE l.conversation_id = c.conversation_id)",
                        (hygiene_cutoff,)).fetchall()
                    empty_ids = [r["conversation_id"] for r in empty]
                    for conversation_id in empty_ids:
                        self._delete_conversation_rows(self.conn, conversation_id)
            except sqlite3.Error as exc:
                raise ConversationStoreError(f"purge_expired failed: {exc}") from exc
        return {"conversations": len(ids), "messages": messages,
                "empty_explicit": len(empty_ids), "retention_days": self.retention_days}

    def purge_all(self) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                for table in (*_CHILD_TABLES, "conversations"):
                    self.conn.execute(f"DELETE FROM {table}")
