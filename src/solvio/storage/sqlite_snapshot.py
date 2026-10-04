"""Konsistente Schnappschuesse lebender SQLite-Speicher.

`cp` ist hier falsch, und zwar nicht theoretisch. Jeder SOLVIO-Speicher laeuft
im WAL-Modus: die Datei `x.sqlite3` ist dann nur die halbe Wahrheit, der Rest
steht in `x.sqlite3-wal`. Wer die drei Dateien nacheinander kopiert, kopiert
drei verschiedene Zeitpunkte. Das Ergebnis oeffnet sich meistens und ist
manchmal falsch — die unangenehmste aller Sicherungsarten.

Benutzt wird die **Online-Backup-API** von SQLite (`Connection.backup`). Sie ist
genau dafuer gemacht: sie haelt einen konsistenten Lesestand, und wenn waehrend
des Kopierens jemand schreibt, faengt sie von vorn an. Das Ziel bekommt bewusst
KEIN WAL — eine Sicherung ist eine Datei, nicht drei.

Eine gemessene Eigenheit, die dieses Modul praegt
-------------------------------------------------
Nicht jeder Speicher laesst sich schreibgeschuetzt oeffnen. Ein WAL-Speicher
braucht seinen `-shm`-Index; existiert der gerade nicht, weil kein Prozess die
Datei offen haelt, dann muss SQLite ihn ANLEGEN — und genau das verbietet
`mode=ro`. Bei der Storage-V1-Messung (2026-08-27) scheiterten daran
`proactive.sqlite3` und `doctor.sqlite3` (beide werden pro Vorgang geoeffnet
und geschlossen). Das ist eine LAGE, kein Dauerzustand: seit der Backup-Job
als eigener launchd-Prozess laeuft, zeigen die Manifeste fuer ALLE Speicher
`opened_readonly: true` (gemessen 2026-08-31, DEBT-0162). Der Rueckfallweg
bleibt trotzdem noetig — er greift immer dann, wenn gerade kein Prozess die
Datei offen haelt.

Also: erst schreibgeschuetzt versuchen, und nur wenn das an genau diesem Punkt
scheitert, normal oeffnen. Geschrieben wird auch dann nichts — die Backup-API
liest nur. Welcher Weg genommen wurde, steht im Ergebnis und damit im Manifest;
eine Sicherung, die ihren eigenen Weg verschweigt, ist nicht pruefbar.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Any

_CHUNK = 1 << 20

#: Seiten je Schritt der Backup-API. Klein genug, dass ein Schreiber dazwischen
#: kommt, statt ausgesperrt zu werden — die Sicherung ist der Gast.
_BACKUP_PAGES = 256

#: Frist fuer eine gesperrte Datenbank. Laenger als jede echte Transaktion in
#: diesem System, kuerzer als die Geduld eines Menschen.
_TIMEOUT = 30.0


class SnapshotError(RuntimeError):
    """Der Schnappschuss ist nicht zustandegekommen oder nicht gueltig."""


@dataclass(frozen=True)
class SnapshotResult:
    """Was tatsaechlich passiert ist. Zahlen und Struktur, nie Inhalt."""

    source: str
    dest: str
    bytes: int
    sha256: str
    integrity: str
    opened_readonly: bool
    source_journal_mode: str | None
    tables: dict[str, int] = field(default_factory=dict)
    #: Schemastand (§8 Offsite V1): Tabellenzaehler allein erkennen einen
    #: Restore in einen AELTEREN Codestand nicht. `PRAGMA user_version` und —
    #: falls der Speicher eine fuehrt — der Stand der Migrationstabelle machen
    #: Schema-Drift vergleichbar, ohne Inhalt zu tragen.
    user_version: int = 0
    schema_stand: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_source(path: str) -> tuple[sqlite3.Connection, bool]:
    """Oeffnet die Quelle so schreibgeschuetzt wie moeglich.

    Gibt (Verbindung, war_schreibgeschuetzt) zurueck.
    """
    uri = f"file:{_uri_escape(path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT)
        # `connect` liest den Dateikopf nicht. Erst eine echte Abfrage zeigt,
        # ob der WAL-Index fehlt — das ist der Punkt, an dem `mode=ro` scheitert.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, True
    except sqlite3.Error:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
    conn = sqlite3.connect(path, timeout=_TIMEOUT)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise SnapshotError(f"cannot open source {path}: {exc}") from exc
    return conn, False


def _uri_escape(path: str) -> str:
    """SQLite-URI-Pfade: `?` und `#` trennen, also muessen sie kodiert werden.

    Kein `urllib.quote`: das kodiert auch Leerzeichen zu `%20`, was SQLite
    korrekt liest — aber es kodiert nicht den Fall, auf den es ankommt.
    """
    return path.replace("?", "%3f").replace("#", "%23")


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Tabellenname -> Zeilenzahl. Struktur und Menge, nie Inhalt.

    Genau das braucht eine Wiederherstellungsprobe, um mehr zu behaupten als
    „die Datei liess sich oeffnen".
    """
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    for (name,) in rows:
        # Der Name kommt aus sqlite_master, nicht von aussen; trotzdem wird er
        # als Bezeichner zitiert und nicht eingesetzt.
        quoted = '"' + str(name).replace('"', '""') + '"'
        try:
            counts[str(name)] = int(
                conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        except sqlite3.Error:
            # Eine FTS-Schattentabelle kann sich der Zaehlung verweigern. Das
            # ist kein Fehler der Sicherung.
            counts[str(name)] = -1
    return counts


#: Migrationstabellen, die SOLVIO-Speicher fuehren. Wer eine dritte einfuehrt,
#: traegt sie hier ein — sonst bleibt ihr Stand im Manifest unsichtbar.
_MIGRATION_TABLES = ("schema_migrations", "schema_meta")


def _schema_stand(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Stand der Migrationstabelle, falls der Speicher eine fuehrt.

    Zeilenzahl plus Pruefsumme ueber die sortierten Zeilen: vergleichbar
    (der Restore-Beweis prueft Schemastand gegen Codestand, §8), aber ohne
    die Zeilen selbst ins Manifest zu tragen.
    """
    for name in _MIGRATION_TABLES:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        if row is None:
            continue
        try:
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        except sqlite3.Error:
            return {"table": name, "rows": -1, "sha256": None}
        digest = hashlib.sha256(
            repr(sorted(tuple(r) for r in rows)).encode("utf-8")).hexdigest()
        return {"table": name, "rows": len(rows), "sha256": digest}
    return None


def snapshot(source: str, dest: str) -> SnapshotResult:
    """Konsistenter Einzeldatei-Schnappschuss eines lebenden SQLite-Speichers."""
    if not os.path.exists(source):
        raise SnapshotError(f"source missing: {source}")
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    for path in (dest, dest + "-wal", dest + "-shm"):
        if os.path.exists(path):
            os.remove(path)

    src, readonly = _open_source(source)
    journal_mode: str | None = None
    try:
        try:
            journal_mode = str(src.execute("PRAGMA journal_mode").fetchone()[0])
        except sqlite3.Error:
            journal_mode = None
        dst = sqlite3.connect(dest, timeout=_TIMEOUT)
        try:
            src.backup(dst, pages=_BACKUP_PAGES)
        finally:
            dst.close()
    finally:
        src.close()

    # Erst jetzt urteilen — und zwar ueber die SICHERUNG, nicht ueber die
    # Quelle. Eine Sicherung, die man nicht oeffnen kann, ist keine.
    verify = sqlite3.connect(dest, timeout=_TIMEOUT)
    try:
        # Die Backup-API kopiert SEITEN, und dazu gehoert der Dateikopf. Eine
        # Sicherung einer WAL-Datenbank traegt deshalb selbst WAL im Kopf — und
        # ist damit genau so schreibgeschuetzt unoeffenbar wie die Quellen, an
        # denen dieses Modul schon einmal gestolpert ist. Der Schalter hier ist
        # kein Aufraeumen, er ist die Zusicherung: eine Sicherung ist EINE
        # Datei, und man kann sie lesen, ohne sie anfassen zu duerfen.
        verify.execute("PRAGMA journal_mode=DELETE")
        integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise SnapshotError(f"snapshot of {source} failed integrity_check: {integrity}")
        tables = _table_counts(verify)
        # Geurteilt wird ueber die SICHERUNG — auch beim Schemastand.
        user_version = int(verify.execute("PRAGMA user_version").fetchone()[0])
        schema_stand = _schema_stand(verify)
    finally:
        verify.close()

    # Das Ziel darf keine Begleitdateien haben: eine Sicherung ist eine Datei.
    for sfx in ("-wal", "-shm"):
        stray = dest + sfx
        if os.path.exists(stray):
            os.remove(stray)

    os.chmod(dest, 0o600)
    return SnapshotResult(
        source=source,
        dest=dest,
        bytes=os.path.getsize(dest),
        sha256=sha256_file(dest),
        integrity=integrity,
        opened_readonly=readonly,
        source_journal_mode=journal_mode,
        tables=tables,
        user_version=user_version,
        schema_stand=schema_stand,
    )


def verify_snapshot(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Prueft eine Sicherungsdatei fuer sich allein — ohne die Quelle.

    Fail-closed: was nicht nachweisbar in Ordnung ist, ist ein Fehler.
    """
    errors: list[str] = []
    if not os.path.exists(path):
        return {"ok": False, "errors": [f"missing: {path}"]}
    actual = sha256_file(path)
    if expected_sha256 and actual != expected_sha256:
        errors.append(f"checksum mismatch for {os.path.basename(path)}")
    conn = None
    tables: dict[str, int] = {}
    try:
        # Schreibgeschuetzt, und zwar wirklich: eine Pruefung, die ihr Pruefstueck
        # anfassen muss, ist keine. Faellt das aus, ist die Sicherung kaputt —
        # hier gibt es bewusst KEINEN Rueckfallweg wie beim Sichern der Quellen.
        conn = sqlite3.connect(f"file:{_uri_escape(path)}?mode=ro", uri=True,
                               timeout=_TIMEOUT)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"integrity_check: {integrity}")
        tables = _table_counts(conn)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema_stand = _schema_stand(conn)
    except sqlite3.DatabaseError as exc:
        errors.append(f"cannot open: {exc}")
        user_version = 0
        schema_stand = None
    finally:
        if conn is not None:
            conn.close()
    return {"ok": not errors, "errors": errors, "sha256": actual, "tables": tables,
            "user_version": user_version, "schema_stand": schema_stand}
