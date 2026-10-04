"""Das Offsite-Buch — SOLVIOs eigene Wahrheit ueber jede Generation.

`~/.solvio/offsite.sqlite3` (WAL, 0600). **Der Anbieter ist Speicher, nicht
Autoritaet** (Vertrag §17): was wirklich existiert, sagt dieses Buch; was AWS
antwortet, wird als *Provider-Wahrheit* danebengeschrieben (VersionId, ETag,
Groesse) und nie damit verwechselt.

Der Ort ist mit Absicht gewaehlt: Das Buch liegt OBEN als
`~/.solvio/offsite.sqlite3`, weil es Bestand ist und gesichert wird (es steht
selbst in `inventory.items()` — die DEBT-0130-Lektion, beim Bau gleich
richtig). Der Ordner `~/.solvio/offsite/` daneben traegt Betriebszustand und
Staging und wird NICHT gesichert. Dieselbe Trennung wie bei `storage/`.

**Die Zustandsmaschine hat keinen Weg, an dem Unwissen wie Erfolg aussieht.**
Eine Generation entsteht als `prepared` und wird VOR dem ersten Byte auf
`uploading` gesetzt. Ein Prozess, der danach stirbt (Absturz, Stromausfall,
`kill`), hinterlaesst eine Zeile in `uploading` — und `uploading` ist beim
naechsten Start **kein Erfolg**, sondern eine offene Frage, die der Job
beantwortet (`recover_open()`), statt sie zu erben. `uploaded` heisst: der
PUT ist vollstaendig zurueckgekommen. `verified` heisst zusaetzlich: das
Objekt wurde zurueckgeladen und Byte fuer Byte gegen `cipher_sha256`
gerechnet. Nur `verified` ist ein Erfolg im Sinne des Vertrags.

Was hier NIE steht (§17): Geheimniswerte, Inhalte, Tabellenzaehler. Zaehler
stehen im Manifest, und das Manifest liegt in der Huelle. Die
Access-Key-**ID** darf stehen — sie ist eine Kennung, kein Geheimnis (§16).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from solvio.logging_setup import get_logger

log = get_logger("offsite")

DEFAULT_PATH = "~/.solvio/offsite.sqlite3"

#: Zeilendeckel je Tabelle (Muster Vault-Ledger, §17). Ein Buch, das
#: unbegrenzt waechst, ist eine Zusage, die man nicht halten kann.
MAX_ROWS = 5000

SCHEMA_VERSION = 1


# --------------------------------------------------------------- die Zustaende
#: Eine Generation ist vorbereitet: Staging gepackt, Huelle geschrieben,
#: Pruefsumme gerechnet. Noch KEIN Byte beim Anbieter.
PREPARED = "prepared"
#: Der Upload laeuft. Wird VOR dem ersten Byte gesetzt und ueberlebt einen
#: Absturz — genau dafuer ist er da.
UPLOADING = "uploading"
#: Der PUT ist vollstaendig zurueckgekommen (und die Klassen-Kopien auch).
UPLOADED = "uploaded"
#: Zurueckgeladen und gegen `cipher_sha256` nachgerechnet. Der einzige Erfolg.
VERIFIED = "verified"
#: Gescheitert, mit genau einer Kategorie aus §18.
FAILED = "failed"

#: Die geschlossene Menge. Ein Zustand ausserhalb ist ein Programmierfehler,
#: kein Datenzustand — deshalb wirft `_check_state` statt zu raten.
STATES = (PREPARED, UPLOADING, UPLOADED, VERIFIED, FAILED)

#: Zustaende, die einen unterbrochenen Lauf bezeichnen. Sie sind NIE ein
#: Erfolg und werden beim naechsten Start aufgeraeumt.
OPEN_STATES = (PREPARED, UPLOADING)

#: Was als erfolgreiche Generation zaehlt — fuer die Tagesbremse (§11) und
#: die GFS-Klassenwahl (§9). `uploaded` zaehlt mit: der Ruecklade-Vergleich
#: haengt daran, aber ein zweiter Upload desselben Tages waere Verschwendung.
SUCCESS_STATES = (UPLOADED, VERIFIED)


class LedgerError(RuntimeError):
    """Das Buch konnte nicht gefuehrt werden."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ledger_path() -> str:
    return os.path.expanduser(os.environ.get("SOLVIO_OFFSITE_LEDGER")
                              or DEFAULT_PATH)


def _check_state(state: str) -> str:
    if state not in STATES:
        raise LedgerError(f"unknown state: {state!r}")
    return state


@dataclass(frozen=True)
class Generation:
    """Eine Zeile des Buches, so wie sie gelesen wird."""

    generation_id: str
    state: str
    classes: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    snapshot_manifest_sha256: str = ""
    plain_bytes: int = 0
    cipher_bytes: int = 0
    cipher_sha256: str = ""
    recipient_fingerprint: str = ""
    source_ok: bool = True
    object_keys: dict[str, Any] = field(default_factory=dict)
    failure_category: str = ""
    error: str = ""
    access_key_id: str = ""

    @property
    def succeeded(self) -> bool:
        return self.state in SUCCESS_STATES

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["classes"] = list(self.classes)
        return data


class OffsiteLedger:
    """Das Buch. Oeffnet je Vorgang, schreibt sofort, glaubt nichts."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or ledger_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                    mode=0o700, exist_ok=True)
        self._initialise()

    # -- Aufbau ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialise(self) -> None:
        fresh = not os.path.exists(self.path)
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS offsite_generations (
                    generation_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    classes TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    snapshot_manifest_sha256 TEXT NOT NULL DEFAULT '',
                    plain_bytes INTEGER NOT NULL DEFAULT 0,
                    cipher_bytes INTEGER NOT NULL DEFAULT 0,
                    cipher_sha256 TEXT NOT NULL DEFAULT '',
                    recipient_fingerprint TEXT NOT NULL DEFAULT '',
                    source_ok INTEGER NOT NULL DEFAULT 1,
                    object_keys TEXT NOT NULL DEFAULT '{}',
                    failure_category TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    access_key_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS offsite_verifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    result TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS offsite_retention (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    sweep_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_generations_state
                    ON offsite_generations(state);
                CREATE INDEX IF NOT EXISTS idx_generations_started
                    ON offsite_generations(started_at);
            """)
            if fresh:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        if fresh:
            os.chmod(self.path, 0o600)

    # -- Generationen ------------------------------------------------------
    def start(self, *, generation_id: str, classes: Iterable[str],
              snapshot_manifest_sha256: str = "", plain_bytes: int = 0,
              cipher_bytes: int = 0, cipher_sha256: str = "",
              recipient_fingerprint: str = "", source_ok: bool = True,
              access_key_id: str = "") -> Generation:
        """Traegt eine vorbereitete Generation ein — VOR dem ersten Byte.

        Idempotent auf der `generation_id`: derselbe Lauf, zweimal
        angestossen, erzeugt keine zweite Wahrheit (§ „idempotente
        Generationen"). Eine bereits ERFOLGREICHE Generation wird dabei nie
        ueberschrieben — sie wird zurueckgegeben.
        """
        existing = self.get(generation_id)
        if existing is not None and existing.state in SUCCESS_STATES:
            return existing
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO offsite_generations
                    (generation_id, state, classes, started_at,
                     snapshot_manifest_sha256, plain_bytes, cipher_bytes,
                     cipher_sha256, recipient_fingerprint, source_ok,
                     access_key_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(generation_id) DO UPDATE SET
                    state=excluded.state,
                    classes=excluded.classes,
                    started_at=excluded.started_at,
                    snapshot_manifest_sha256=excluded.snapshot_manifest_sha256,
                    plain_bytes=excluded.plain_bytes,
                    cipher_bytes=excluded.cipher_bytes,
                    cipher_sha256=excluded.cipher_sha256,
                    recipient_fingerprint=excluded.recipient_fingerprint,
                    source_ok=excluded.source_ok,
                    access_key_id=excluded.access_key_id,
                    finished_at='', failure_category='', error=''
            """, (generation_id, PREPARED, ",".join(classes), now,
                  snapshot_manifest_sha256, int(plain_bytes),
                  int(cipher_bytes), cipher_sha256, recipient_fingerprint,
                  1 if source_ok else 0, access_key_id))
            conn.commit()
        finally:
            conn.close()
        self._trim("offsite_generations", "started_at")
        return self.get(generation_id)  # type: ignore[return-value]

    def mark(self, generation_id: str, state: str, *,
             object_keys: dict[str, Any] | None = None,
             failure_category: str = "", error: str = "") -> None:
        """Setzt den Zustand. `uploaded`/`verified`/`failed` sind Endpunkte.

        Der Fehlertext wird auf 400 Zeichen gekuerzt (§17) — ein Buch ist
        kein Protokoll, und eine lange Ausnahme traegt gern mehr mit sich,
        als sie soll.
        """
        _check_state(state)
        finished = utcnow_iso() if state in (UPLOADED, VERIFIED, FAILED) else ""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT object_keys FROM offsite_generations "
                "WHERE generation_id=?", (generation_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown generation: {generation_id}")
            keys = json.loads(row["object_keys"] or "{}")
            if object_keys:
                keys.update(object_keys)
            conn.execute("""
                UPDATE offsite_generations
                   SET state=?, finished_at=?, object_keys=?,
                       failure_category=?, error=?
                 WHERE generation_id=?
            """, (state, finished, json.dumps(keys, sort_keys=True),
                  failure_category, (error or "")[:400], generation_id))
            conn.commit()
        finally:
            conn.close()
        log.info("offsite.ledger_state", generation=generation_id,
                 state=state, category=failure_category or "")

    def get(self, generation_id: str) -> Generation | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM offsite_generations WHERE generation_id=?",
                (generation_id,)).fetchone()
        finally:
            conn.close()
        return _row_to_generation(row) if row is not None else None

    def recent(self, limit: int = 50) -> list[Generation]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM offsite_generations "
                "ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            conn.close()
        return [_row_to_generation(r) for r in rows]

    def open_generations(self) -> list[Generation]:
        """Zeilen, die ein unterbrochener Lauf hinterlassen hat.

        Genau das ist die ehrliche Rekonstruktion nach einem Absturz: nicht
        „war wohl gut", sondern „hier steht eine offene Frage"."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM offsite_generations WHERE state IN "
                f"({placeholders}) ORDER BY started_at", OPEN_STATES).fetchall()
        finally:
            conn.close()
        return [_row_to_generation(r) for r in rows]

    def successful_on(self, utc_day: str) -> Generation | None:
        """Gibt es fuer diesen UTC-Kalendertag schon eine gute Generation?

        Die Tagesbremse aus §11 haengt hier — und sie fragt nach dem
        KALENDERTAG, nicht nach verstrichener Zeit: sonst wandert die
        Laufzeit rueckwaerts und „1 je UTC-Kalendertag" bricht.
        """
        placeholders = ",".join("?" for _ in SUCCESS_STATES)
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT * FROM offsite_generations "
                f"WHERE state IN ({placeholders}) AND generation_id LIKE ? "
                f"ORDER BY started_at DESC LIMIT 1",
                (*SUCCESS_STATES, f"{utc_day}T%")).fetchone()
        finally:
            conn.close()
        return _row_to_generation(row) if row is not None else None

    def has_success_in(self, *, since: str, until: str) -> bool:
        """Gab es im Zeitfenster [since, until) eine gute Generation?

        Fuer die GFS-Klassenwahl (§9): ist die heutige Generation die erste
        ihrer ISO-Woche bzw. ihres Monats, bekommt sie zusaetzlich die
        weekly-/monthly-Klasse.
        """
        placeholders = ",".join("?" for _ in SUCCESS_STATES)
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT 1 FROM offsite_generations "
                f"WHERE state IN ({placeholders}) "
                f"AND generation_id >= ? AND generation_id < ? LIMIT 1",
                (*SUCCESS_STATES, since, until)).fetchone()
        finally:
            conn.close()
        return row is not None

    # -- Verifikationen und Retention --------------------------------------
    def record_verification(self, *, generation_id: str, kind: str,
                            result: str, duration_seconds: float = 0.0,
                            details: dict[str, Any] | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO offsite_verifications
                    (at, generation_id, kind, result, duration_seconds, details)
                VALUES (?,?,?,?,?,?)
            """, (utcnow_iso(), generation_id, kind, result,
                  float(duration_seconds),
                  json.dumps(details or {}, sort_keys=True)))
            conn.commit()
        finally:
            conn.close()
        self._trim("offsite_verifications", "at")

    def record_retention(self, *, generation_id: str, event: str,
                         sweep_id: str = "") -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO offsite_retention
                    (observed_at, generation_id, event, sweep_id)
                VALUES (?,?,?,?)
            """, (utcnow_iso(), generation_id, event, sweep_id))
            conn.commit()
        finally:
            conn.close()
        self._trim("offsite_retention", "observed_at")

    def verifications(self, generation_id: str = "",
                      limit: int = 50) -> list[dict[str, Any]]:
        """Verifikationen, neueste zuerst.

        Sortiert nach `at DESC, id DESC` — und das zweite Kriterium ist kein
        Beiwerk: `at` hat Sekundenaufloesung, und zwei Beweise in derselben
        Sekunde sind keine Theorie (ein bestandener Ruecklade-Vergleich und
        ein gescheiterter Restore-Beweis koennen unmittelbar aufeinander
        folgen). Ohne den Tiebreak gewinnt der AELTERE, und die Gesundheit
        liest einen Erfolg, wo ein Fehlschlag daneben steht. Genau das hat
        der eigene Test gefunden.
        """
        conn = self._connect()
        try:
            if generation_id:
                rows = conn.execute(
                    "SELECT * FROM offsite_verifications WHERE generation_id=? "
                    "ORDER BY at DESC, id DESC LIMIT ?",
                    (generation_id, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM offsite_verifications "
                    "ORDER BY at DESC, id DESC LIMIT ?",
                    (int(limit),)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # -- Deckel ------------------------------------------------------------
    def _trim(self, table: str, order_column: str) -> None:
        """Kappt auf `MAX_ROWS`, aelteste zuerst. Muster des Vault-Ledgers."""
        conn = self._connect()
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count <= MAX_ROWS:
                return
            key = ("generation_id" if table == "offsite_generations" else "id")
            conn.execute(f"""
                DELETE FROM {table} WHERE {key} IN (
                    SELECT {key} FROM {table}
                    ORDER BY {order_column} ASC LIMIT ?)
            """, (count - MAX_ROWS,))
            conn.commit()
        finally:
            conn.close()


def _row_to_generation(row: sqlite3.Row) -> Generation:
    return Generation(
        generation_id=str(row["generation_id"]),
        state=str(row["state"]),
        classes=tuple(c for c in str(row["classes"]).split(",") if c),
        started_at=str(row["started_at"] or ""),
        finished_at=str(row["finished_at"] or ""),
        snapshot_manifest_sha256=str(row["snapshot_manifest_sha256"] or ""),
        plain_bytes=int(row["plain_bytes"] or 0),
        cipher_bytes=int(row["cipher_bytes"] or 0),
        cipher_sha256=str(row["cipher_sha256"] or ""),
        recipient_fingerprint=str(row["recipient_fingerprint"] or ""),
        source_ok=bool(row["source_ok"]),
        object_keys=json.loads(str(row["object_keys"] or "{}")),
        failure_category=str(row["failure_category"] or ""),
        error=str(row["error"] or ""),
        access_key_id=str(row["access_key_id"] or ""),
    )
