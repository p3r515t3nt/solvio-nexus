"""Die Ablage — Geheimtext und Policy in EINER Datei, Klartext in keiner.

Warum eine SQLite-Datei und nicht je Geheimnis eine Datei: weil Policy und
Geheimtext zusammen geaendert werden muessen. Eine Rotation, die den neuen
Geheimtext schreibt und die Fassungsnummer nicht, hinterlaesst einen Eintrag,
der nie wieder aufgeht. In einer Transaktion gibt es diesen Zwischenzustand
nicht.

Warum der Geheimtext IN der Datenbank und nicht daneben: damit es genau einen
Ort gibt, den man sichern, pruefen und loeschen kann. Der Geheimtext ist ohne den
Schluesselbund wertlos (das ist der ganze Punkt), also kostet seine Naehe zur
Policy nichts.

Was hier NICHT passiert: entschluesseln. Diese Schicht kennt keinen Schluessel.
Sie legt Bytes ab und gibt Bytes zurueck. Wer sie oeffnet, ist
`solvio.secret_vault.broker` — und nur der.

Zur Zugriffsspur: sie steht in derselben Datei, aber in einer eigenen Tabelle,
und sie ist ausdruecklich NICHT das kuenftige Agent Run Ledger. Sie beantwortet
eine einzige Frage: welcher Zugang wurde wann von welchem Executor fuer welches
Ziel benutzt. Kein Wert, kein Anfragekoerper, keine Kopfzeile.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Iterable, Mapping

from solvio.logging_setup import get_logger
from solvio.secret_vault.envelope import Sealed
from solvio.secret_vault.policy import SecretPolicy, Status, from_row

log = get_logger("vault")

#: Eigenes Verzeichnis, eigene Rechte. Nicht unter `~/.solvio/`, damit die
#: Aussage „der Tresor ist genau dieser Pfad" ohne Aufzaehlung stimmt — das
#: erleichtert die Seatbelt-Zusicherung und die Rechtepruefung.
DEFAULT_DIR = "~/.solvio-vault"
DB_NAME = "vault.sqlite3"

#: Testschalter. Ein Test darf nie in den produktiven Tresor schreiben.
DIR_ENV = "SOLVIO_VAULT_DIR"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS secrets (
    secret_ref             TEXT PRIMARY KEY,
    kind                   TEXT NOT NULL,
    version                INTEGER NOT NULL,
    status                 TEXT NOT NULL,
    allowed_capabilities   TEXT NOT NULL DEFAULT '',
    allowed_targets        TEXT NOT NULL DEFAULT '',
    allowed_executors      TEXT NOT NULL DEFAULT '',
    allow_background       INTEGER NOT NULL DEFAULT 0,
    requires_user_presence INTEGER NOT NULL DEFAULT 0,
    display_name           TEXT NOT NULL DEFAULT '',
    service_label          TEXT NOT NULL DEFAULT '',
    account_label          TEXT NOT NULL DEFAULT '',
    note                   TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    rotated_at             TEXT NOT NULL DEFAULT '',
    last_used_at           TEXT NOT NULL DEFAULT '',
    envelope_version       INTEGER NOT NULL,
    wrapped_dek            BLOB NOT NULL,
    ciphertext             BLOB NOT NULL,
    policy_sha256          TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS access_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    secret_ref     TEXT NOT NULL,
    secret_version INTEGER NOT NULL DEFAULT 0,
    capability     TEXT NOT NULL DEFAULT '',
    origin         TEXT NOT NULL DEFAULT '',
    executor       TEXT NOT NULL DEFAULT '',
    target         TEXT NOT NULL DEFAULT '',
    approval_id    TEXT NOT NULL DEFAULT '',
    execution_id   TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL,
    denied_reason  TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_ledger_ref ON access_ledger(secret_ref, id DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_at ON access_ledger(at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL);
"""

#: Wie viele Zeilen die Zugriffsspur haelt. Eine Spur, die nie vergisst, wird
#: zur Halde — und eine Halde liest niemand.
LEDGER_KEEP = 5000

OUTCOME_USED = "used"
OUTCOME_DENIED = "denied"
OUTCOME_FAILED = "failed"
OUTCOME_MUTATED = "mutated"


def vault_dir() -> str:
    return os.path.expanduser(os.environ.get(DIR_ENV) or DEFAULT_DIR)


def db_path() -> str:
    return os.path.join(vault_dir(), DB_NAME)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _join(values: Iterable[Any]) -> str:
    return "\n".join(str(v.value if hasattr(v, "value") else v) for v in values)


class VaultStoreError(RuntimeError):
    """Die Ablage konnte nicht bedient werden. Traegt nie einen Wert."""


class VaultStore:
    """Geheimtext und Policy. Kennt keinen Schluessel und oeffnet nichts."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or db_path()
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        previous = os.umask(0o077)
        try:
            with self._open() as connection:
                connection.executescript(SCHEMA)
        finally:
            os.umask(previous)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht.

        Dieselbe Lehre wie beim Hintergrundspeicher: `with sqlite3.connect(...)`
        ist ein Transaktionskontext und schliesst NICHTS.
        """
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    # -- Rechte --------------------------------------------------------------
    def permissions_ok(self) -> bool:
        """Steht die Datei fuer Gruppe oder andere offen?

        Nicht reparieren, sondern melden: Rechte, die einmal offen standen,
        koennten bereits gelesen worden sein. Dieselbe Haltung wie im
        Portaltresor.
        """
        try:
            mode = os.stat(self.path).st_mode & 0o777
        except OSError:
            return False
        return not (mode & 0o077)

    # -- Lesen ---------------------------------------------------------------
    def refs(self, *, include_inactive: bool = True) -> list[str]:
        query = "SELECT secret_ref FROM secrets"
        if not include_inactive:
            query += " WHERE status = 'active'"
        with self._open() as connection:
            return [r["secret_ref"] for r in
                    connection.execute(query + " ORDER BY secret_ref")]

    def row(self, secret_ref: str) -> dict[str, Any] | None:
        """Eine Zeile als gewoehnliches Woerterbuch.

        Bewusst nicht als `sqlite3.Row`: der Typ hat kein `.get()`, und jede
        Stelle, die eine Zeile weiterreicht, muesste das wissen. Ein Ergebnis,
        das sich nur mit Sonderwissen benutzen laesst, wird irgendwo falsch
        benutzt.
        """
        with self._open() as connection:
            cursor = connection.execute(
                "SELECT * FROM secrets WHERE secret_ref = ?", (secret_ref,))
            found = cursor.fetchone()
            return dict(found) if found is not None else None

    def policy(self, secret_ref: str) -> SecretPolicy | None:
        row = self.row(secret_ref)
        return None if row is None else from_row(row)

    def policies(self) -> list[SecretPolicy]:
        with self._open() as connection:
            return [from_row(dict(r)) for r in
                    connection.execute("SELECT * FROM secrets ORDER BY secret_ref")]

    def sealed(self, secret_ref: str) -> tuple[Sealed, str] | None:
        """Der Umschlag und die GESPEICHERTE Policy-Pruefsumme.

        Beide zusammen, damit der Aufrufer die gespeicherte gegen die gerechnete
        halten kann. Stimmen sie nicht ueberein, wurde an der Zeile gedreht — und
        dann wird gar nicht erst entschluesselt.
        """
        row = self.row(secret_ref)
        if row is None:
            return None
        return (Sealed(int(row["envelope_version"]),
                       bytes(row["wrapped_dek"]), bytes(row["ciphertext"])),
                str(row["policy_sha256"]))

    def count(self) -> int:
        with self._open() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) AS n FROM secrets").fetchone()["n"])

    # -- Schreiben -----------------------------------------------------------
    def put(self, policy: SecretPolicy, sealed: Sealed) -> None:
        """Legt einen Eintrag an oder ersetzt ihn — in EINER Transaktion.

        Policy und Umschlag gehoeren zusammen: der Umschlag ist an die
        Policy-Pruefsumme gebunden. Getrennt geschrieben ergaebe der
        Zwischenzustand einen Eintrag, der sich nie wieder oeffnen laesst.
        """
        fields = policy.authority_fields()
        digest = policy.digest()
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO secrets (
                        secret_ref, kind, version, status,
                        allowed_capabilities, allowed_targets, allowed_executors,
                        allow_background, requires_user_presence,
                        display_name, service_label, account_label, note,
                        created_at, rotated_at, last_used_at,
                        envelope_version, wrapped_dek, ciphertext, policy_sha256)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(secret_ref) DO UPDATE SET
                        kind=excluded.kind, version=excluded.version,
                        status=excluded.status,
                        allowed_capabilities=excluded.allowed_capabilities,
                        allowed_targets=excluded.allowed_targets,
                        allowed_executors=excluded.allowed_executors,
                        allow_background=excluded.allow_background,
                        requires_user_presence=excluded.requires_user_presence,
                        display_name=excluded.display_name,
                        service_label=excluded.service_label,
                        account_label=excluded.account_label,
                        note=excluded.note,
                        rotated_at=excluded.rotated_at,
                        envelope_version=excluded.envelope_version,
                        wrapped_dek=excluded.wrapped_dek,
                        ciphertext=excluded.ciphertext,
                        policy_sha256=excluded.policy_sha256""",
                    (policy.secret_ref, policy.kind.value, int(policy.version),
                     policy.status.value,
                     _join(fields["allowed_capabilities"]),
                     _join(fields["allowed_targets"]),
                     _join(fields["allowed_executors"]),
                     1 if policy.allow_background else 0,
                     1 if policy.requires_user_presence else 0,
                     policy.display_name, policy.service_label,
                     policy.account_label, policy.note,
                     policy.created_at or utcnow_iso(), policy.rotated_at,
                     policy.last_used_at,
                     int(sealed.envelope_version), sealed.wrapped_dek,
                     sealed.ciphertext, digest))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def set_status(self, secret_ref: str, status: Status,
                   *, sealed: Sealed, policy: SecretPolicy) -> None:
        """Zustandswechsel — mit NEUEM Umschlag, weil der Zustand im AAD steht.

        Das ist kein Umweg. Genau weil der Zustand mitversiegelt ist, kann
        niemand eine widerrufene Zeile in der Datenbank auf `active` drehen und
        sie danach benutzen.
        """
        self.put(policy, sealed)
        log.info("vault.status_changed", secret_ref=secret_ref, status=status.value)

    def rotate_if_current(self, policy: SecretPolicy, sealed: Sealed, *,
                          expected: Mapping[str, SecretPolicy]) -> SecretPolicy:
        """Owner-Login: gleiche Rechte, zwei gebundene Zugaenge, atomarer CAS.

        Der Google-Client und sein Refresh-Zugang muessen noch genau die
        Version/Policy vom Start der Anmeldung haben. Anzeige und last_used_at
        koennen inzwischen weiterlaufen; sie werden absichtlich nicht ersetzt.
        """
        previous = expected.get(policy.secret_ref)
        if (previous is None or previous.status is not Status.ACTIVE
                or replace(previous, version=previous.version + 1).digest()
                != policy.digest()):
            raise VaultStoreError("rotation_binding_invalid")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for ref, before in expected.items():
                    row = connection.execute(
                        "SELECT * FROM secrets WHERE secret_ref = ?", (ref,)).fetchone()
                    if row is None:
                        raise VaultStoreError("rotation_binding_changed")
                    actual = from_row(dict(row))
                    if (actual.secret_ref != before.secret_ref
                            or actual.version != before.version
                            or actual.digest() != before.digest()
                            or row["policy_sha256"] != before.digest()
                            or actual.status is not Status.ACTIVE):
                        raise VaultStoreError("rotation_binding_changed")
                connection.execute(
                    """UPDATE secrets SET version=?, rotated_at=?, envelope_version=?,
                       wrapped_dek=?, ciphertext=?, policy_sha256=? WHERE secret_ref=?""",
                    (policy.version, policy.rotated_at, sealed.envelope_version,
                     sealed.wrapped_dek, sealed.ciphertext, policy.digest(), policy.secret_ref))
                connection.execute(
                    """INSERT INTO access_ledger (at,secret_ref,secret_version,
                       capability,origin,outcome) VALUES (?,?,?,?,?,?)""",
                    (utcnow_iso(), policy.secret_ref, policy.version,
                     "secret_replace", "local_owner", OUTCOME_MUTATED))
                connection.execute(
                    """DELETE FROM access_ledger WHERE id NOT IN
                       (SELECT id FROM access_ledger ORDER BY id DESC LIMIT ?)""", (LEDGER_KEEP,))
                row = connection.execute(
                    "SELECT * FROM secrets WHERE secret_ref=?", (policy.secret_ref,)).fetchone()
                result = from_row(dict(row))
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def rotate_google_pair_if_current(self, values: Mapping[str, tuple[SecretPolicy, Sealed]], *,
                                      expected: Mapping[str, SecretPolicy],
                                      client_id: str, account_label: str) -> None:
        """Store-only CAS for an already-authorized Google client/account switch.

        Both envelopes and their public client ID become visible together.
        Explicit approval may reactivate this pair; its scopes cannot expand.
        This is not an HTTP/agent entry point.
        Existing revoke/delete checks remain on BOTH original secret refs.
        """
        import re
        refs = {"secret://google/oauth-client": "oauth_client_secret",
                "secret://google/refresh": "oauth_refresh_token"}
        if (set(values) != set(refs) or set(expected) != set(refs)
                or not isinstance(client_id, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com", client_id)
                or not isinstance(account_label, str) or not 0 < len(account_label.encode()) <= 320
                or any(ord(c) < 32 or ord(c) == 127 for c in account_label)):
            raise VaultStoreError("google_rotation_binding_invalid")
        for ref, (following, _) in values.items():
            before = expected[ref]
            if (before.secret_ref != ref or before.kind.value != refs[ref]
                    or following.secret_ref != ref
                    or replace(before, version=before.version + 1, status=Status.ACTIVE).digest() != following.digest()):
                raise VaultStoreError("google_rotation_binding_invalid")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for ref, before in expected.items():
                    row = connection.execute("SELECT * FROM secrets WHERE secret_ref=?", (ref,)).fetchone()
                    if row is None:
                        raise VaultStoreError("google_rotation_binding_changed")
                    current = from_row(dict(row))
                    if (current.version != before.version or current.digest() != before.digest()
                            or row["policy_sha256"] != before.digest()):
                        raise VaultStoreError("google_rotation_binding_changed")
                for ref, (following, sealed) in values.items():
                    connection.execute(
                        """UPDATE secrets SET version=?, status=?, rotated_at=?, account_label=?, envelope_version=?,
                           wrapped_dek=?, ciphertext=?, policy_sha256=? WHERE secret_ref=?""",
                        (following.version, following.status.value, following.rotated_at, account_label, sealed.envelope_version,
                         sealed.wrapped_dek, sealed.ciphertext, following.digest(), ref))
                    connection.execute(
                        """INSERT INTO access_ledger (at,secret_ref,secret_version,capability,origin,outcome)
                           VALUES (?,?,?,?,?,?)""",
                        (utcnow_iso(), ref, following.version, "google_connect", "trusted_interactive_app", OUTCOME_MUTATED))
                connection.execute(
                    """INSERT INTO meta (key,value) VALUES ('google_oauth_client_id',?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (client_id,))
                connection.execute(
                    """DELETE FROM access_ledger WHERE id NOT IN
                       (SELECT id FROM access_ledger ORDER BY id DESC LIMIT ?)""", (LEDGER_KEEP,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def delete(self, secret_ref: str) -> bool:
        with self._open() as connection:
            cursor = connection.execute(
                "DELETE FROM secrets WHERE secret_ref = ?", (secret_ref,))
            return cursor.rowcount > 0

    def touch_used(self, secret_ref: str, when: str | None = None) -> None:
        """Nur `last_used_at`. Steht bewusst NICHT im Digest — sonst waere jede
        Benutzung eine Neuversiegelung."""
        with self._open() as connection:
            connection.execute("UPDATE secrets SET last_used_at = ? WHERE secret_ref = ?",
                               (when or utcnow_iso(), secret_ref))

    # -- Zugriffsspur --------------------------------------------------------
    def record(self, *, secret_ref: str, outcome: str, capability: str = "",
               origin: str = "", executor: str = "", target: str = "",
               approval_id: str = "", execution_id: str = "",
               denied_reason: str = "", secret_version: int = 0) -> None:
        """Eine Zeile Wahrheit — und kein Wert darin.

        Die Felder sind einzeln aufgezaehlt und nicht als freies Wörterbuch
        entgegengenommen. Das ist Absicht: ein `**kwargs`-Protokoll ist genau die
        Stelle, an der irgendwann jemand `password=` mitgibt.
        """
        with self._open() as connection:
            connection.execute(
                """INSERT INTO access_ledger (
                    at, secret_ref, secret_version, capability, origin, executor,
                    target, approval_id, execution_id, outcome, denied_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (utcnow_iso(), secret_ref, int(secret_version), capability[:64],
                 origin[:32], executor[:32], target[:128], approval_id[:64],
                 execution_id[:64], outcome[:16], denied_reason[:64]))
            connection.execute(
                """DELETE FROM access_ledger WHERE id NOT IN
                   (SELECT id FROM access_ledger ORDER BY id DESC LIMIT ?)""",
                (LEDGER_KEEP,))

    def ledger(self, *, secret_ref: str = "", limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM access_ledger"
        params: tuple[Any, ...] = ()
        if secret_ref:
            query += " WHERE secret_ref = ?"
            params = (secret_ref,)
        query += " ORDER BY id DESC LIMIT ?"
        params = params + (max(1, min(int(limit), 500)),)
        with self._open() as connection:
            return [dict(r) for r in connection.execute(query, params)]

    # -- Meta ----------------------------------------------------------------
    def meta_get(self, key: str) -> str:
        with self._open() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?",
                                     (key,)).fetchone()
            return str(row["value"]) if row else ""

    def meta_set(self, key: str, value: str) -> None:
        with self._open() as connection:
            connection.execute(
                """INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value))

    def integrity_ok(self) -> bool:
        """Ist die Datei als Datenbank heil? Entschluesselt ausdruecklich nichts."""
        try:
            with self._open() as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        except sqlite3.Error:
            return False


def describe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Was ueber einen Eintrag gesagt werden darf. Diese Liste ist abschliessend.

    Sie steht hier und nicht in der Oberflaeche, damit es genau EINE Stelle gibt,
    an der jemand versehentlich ein Feld hinzufuegt, das einen Wert traegt. Die
    Spalten `wrapped_dek` und `ciphertext` kommen hier nie vor.
    """
    return {
        "secret_ref": str(row["secret_ref"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "version": int(row["version"]),
        "display_name": str(row["display_name"] or ""),
        "service_label": str(row["service_label"] or ""),
        "account_label": str(row["account_label"] or ""),
        "allowed_capabilities": [c for c in str(row["allowed_capabilities"] or "").split("\n") if c],
        "allowed_targets": [t for t in str(row["allowed_targets"] or "").split("\n") if t],
        "allowed_executors": [e for e in str(row["allowed_executors"] or "").split("\n") if e],
        "allow_background": bool(row["allow_background"]),
        "requires_user_presence": bool(row["requires_user_presence"]),
        "created_at": str(row["created_at"] or ""),
        "rotated_at": str(row["rotated_at"] or ""),
        "last_used_at": str(row["last_used_at"] or ""),
        "note": str(row["note"] or ""),
    }
