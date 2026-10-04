"""Die Zahlungsablage — Zahlungsmittel, Absichten und das Zahlungsbuch.

Eine Datei, drei Tabellen, und die Grenze zwischen ihnen ist eine Aussage:

    instruments     WAS bezahlen darf, und in welchen Grenzen
    intents         WAS bezahlt werden soll, und wie weit es gekommen ist
    payment_ledger  WAS tatsaechlich passiert ist — append-only

Warum in einer Datei: eine Zahlung und die Grenze, unter der sie lief, muessen
zusammen geschrieben werden. Wer den Tagesumsatz in einer zweiten Datei fuehrt,
hat nach einem Absturz eine Grenze, die nichts mehr begrenzt.

**Was hier niemals hineingeht:** Kartennummer, Pruefziffer, Magnetstreifendaten,
Bankzugang, Anbieter-Hauptschluessel. Nicht als Spalte, nicht als Freitext,
nicht „nur im Fehlerfall". `firewall.refuse_if_payment_material()` steht vor
jedem Schreibvorgang, der Text aufnimmt — ein Schutz, der nur in einer
Kommentarzeile steht, ist keiner.

**Das Zahlungsbuch ist append-only.** Es gibt kein UPDATE und kein DELETE auf
`payment_ledger`; ein spaeter bekannt gewordener Ausgang ist eine NEUE Zeile.
Ein Buch, das man umschreiben kann, beweist nichts.

**Der Anspruch auf eine Ausfuehrung ist EINDEUTIG.** `UNIQUE(execution_id)` auf
den erfolgreichen Zeilen ist der zweite Zaun neben der Idempotenzkennung des
Anbieters: selbst wenn beide Netzwege doppelt liefen, entsteht keine zweite
Buchung.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from solvio.logging_setup import get_logger
from solvio.payment import refs as PR
from solvio.payment.instruments import (Instrument, InstrumentKind,
                                        InstrumentStatus)
from solvio.payment.intent import (Extra, ExtraKind, LineItem, PaymentIntent,
                                   PaymentIntentError, PaymentQuote, PaymentState)

log = get_logger("payment")

DEFAULT_PATH = "~/.solvio/payments.sqlite3"

#: Testschalter. Ein Test darf nie in das produktive Zahlungsbuch schreiben.
PATH_ENV = "SOLVIO_PAYMENT_DB"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS instruments (
    payment_ref          TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL,
    provider             TEXT NOT NULL,
    status               TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    max_single_minor     INTEGER NOT NULL,
    daily_total_minor    INTEGER NOT NULL DEFAULT 0,
    allowed_currencies   TEXT NOT NULL DEFAULT '',
    allowed_merchant_ids TEXT NOT NULL DEFAULT '',
    provider_secret_ref  TEXT NOT NULL DEFAULT '',
    provider_readonly_ref TEXT NOT NULL DEFAULT '',
    provider_refund_ref  TEXT NOT NULL DEFAULT '',
    provider_token       TEXT NOT NULL DEFAULT '',
    display_name         TEXT NOT NULL DEFAULT '',
    display_hint         TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    last_used_at         TEXT NOT NULL DEFAULT '',
    disabled_reason      TEXT NOT NULL DEFAULT '',
    policy_sha256        TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS intents (
    payment_intent_id           TEXT PRIMARY KEY,
    requested_by                TEXT NOT NULL,
    origin                      TEXT NOT NULL,
    merchant_id                 TEXT NOT NULL,
    merchant_origin             TEXT NOT NULL,
    currency                    TEXT NOT NULL,
    payment_method_ref          TEXT NOT NULL,
    purpose                     TEXT NOT NULL,
    items_json                  TEXT NOT NULL,
    created_at                  REAL NOT NULL,
    expires_at                  REAL NOT NULL,
    agent_expected_total_minor  INTEGER NOT NULL DEFAULT 0,
    shipping_label              TEXT NOT NULL DEFAULT '',
    shipping_destination_sha256 TEXT NOT NULL DEFAULT '',
    recurring                   INTEGER NOT NULL DEFAULT 0,
    state                       TEXT NOT NULL,
    quote_json                  TEXT NOT NULL DEFAULT '',
    execution_id                TEXT NOT NULL DEFAULT '',
    approval_id                 TEXT NOT NULL DEFAULT '',
    agent_run_id                TEXT NOT NULL DEFAULT '',
    task_id                     TEXT NOT NULL DEFAULT '',
    updated_at                  TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intents_ref ON intents(payment_method_ref);

CREATE TABLE IF NOT EXISTS payment_ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    at                 TEXT NOT NULL,
    payment_intent_id  TEXT NOT NULL,
    event              TEXT NOT NULL,
    merchant_id        TEXT NOT NULL DEFAULT '',
    merchant_origin    TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    amount_minor       INTEGER NOT NULL DEFAULT 0,
    currency           TEXT NOT NULL DEFAULT '',
    instrument_ref     TEXT NOT NULL DEFAULT '',
    origin             TEXT NOT NULL DEFAULT '',
    approval_id        TEXT NOT NULL DEFAULT '',
    execution_id       TEXT NOT NULL DEFAULT '',
    provider           TEXT NOT NULL DEFAULT '',
    provider_ref       TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT '',
    refund_status      TEXT NOT NULL DEFAULT '',
    refunded_minor     INTEGER NOT NULL DEFAULT 0,
    failure_category   TEXT NOT NULL DEFAULT '',
    agent_run_id       TEXT NOT NULL DEFAULT '',
    task_id            TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_ledger_intent ON payment_ledger(payment_intent_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_at ON payment_ledger(at DESC);

-- Der zweite Zaun gegen die Doppelbuchung. Genau EINE erfolgreiche Belastung je
-- Ausfuehrungskennung, durchgesetzt von der Datenbank und nicht von einer
-- if-Abfrage, die jemand umstellen kann.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_charge_once
    ON payment_ledger(execution_id)
    WHERE event = 'charged' AND execution_id <> '';

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL);
"""

# -- Ereignisse im Zahlungsbuch. Abschliessend. ------------------------------
EVENT_CREATED = "created"
EVENT_QUOTED = "quoted"
EVENT_APPROVED = "approved"
EVENT_CLAIMED = "claimed"
EVENT_CHARGED = "charged"
EVENT_DECLINED = "declined"
EVENT_FAILED = "failed"
EVENT_AMBIGUOUS = "ambiguous"
EVENT_RECONCILED = "reconciled"
EVENT_CANCELLED = "cancelled"
EVENT_EXPIRED = "expired"
EVENT_REFUNDED = "refunded"
EVENT_DENIED = "denied"
EVENT_INSTRUMENT_CHANGED = "instrument_changed"

ALL_EVENTS = (EVENT_CREATED, EVENT_QUOTED, EVENT_APPROVED, EVENT_CLAIMED,
              EVENT_CHARGED, EVENT_DECLINED, EVENT_FAILED, EVENT_AMBIGUOUS,
              EVENT_RECONCILED, EVENT_CANCELLED, EVENT_EXPIRED, EVENT_REFUNDED,
              EVENT_DENIED, EVENT_INSTRUMENT_CHANGED)


#: Spalten, die eine BESTEHENDE Ablage nachtraegt.
#:
#: `CREATE TABLE IF NOT EXISTS` legt eine fehlende Tabelle an und ruehrt eine
#: vorhandene nicht an — eine neue Spalte kommt damit NIE bei jemandem an, der
#: die Datei schon hat. Das ist keine Vermutung: der erste produktive Start
#: nach dem Hinzufuegen von `provider_refund_ref` scheiterte genau daran, und
#: zwar erst beim Hinterlegen eines Zahlungsmittels — also spaet und an einer
#: Stelle, die nach etwas ganz anderem aussieht.
#:
#: Nur ADDITIV. Es wird nie eine Spalte entfernt und nie eine umgeschrieben:
#: eine Wanderung, die Daten verliert, gehoert in ein eigenes Werkzeug mit
#: eigener Sicherung, nicht in einen Konstruktor.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "instruments": {
        "provider_readonly_ref": "TEXT NOT NULL DEFAULT ''",
        "provider_refund_ref": "TEXT NOT NULL DEFAULT ''",
        "provider_token": "TEXT NOT NULL DEFAULT ''",
    },
    "intents": {
        "agent_run_id": "TEXT NOT NULL DEFAULT ''",
        "task_id": "TEXT NOT NULL DEFAULT ''",
    },
    "payment_ledger": {
        "agent_run_id": "TEXT NOT NULL DEFAULT ''",
        "task_id": "TEXT NOT NULL DEFAULT ''",
    },
}


def _add_missing_columns(connection) -> None:
    """Traegt fehlende Spalten nach. Additiv, still, und ohne Datenverlust."""
    for table, columns in _ADDED_COLUMNS.items():
        try:
            vorhanden = {row["name"] for row in
                         connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            continue
        if not vorhanden:
            continue
        for name, declaration in columns.items():
            if name in vorhanden:
                continue
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            log.info("payment.column_added", table=table, column=name)


def db_path() -> str:
    return os.path.expanduser(os.environ.get(PATH_ENV) or DEFAULT_PATH)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _join(values: Iterable[Any]) -> str:
    return "\n".join(str(v.value if hasattr(v, "value") else v) for v in values)


def _split(text: str) -> tuple[str, ...]:
    return tuple(part for part in (text or "").split("\n") if part)


class PaymentStoreError(RuntimeError):
    """Die Ablage konnte nicht bedient werden. Traegt nie Zahlungsmaterial."""


class DuplicateCharge(PaymentStoreError):
    """Diese Ausfuehrungskennung hat bereits belastet. Es entsteht keine zweite."""


class PaymentStore:
    """Zahlungsmittel, Absichten, Buch. Kennt keinen Anbieter und ruft keinen."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or db_path()
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        previous = os.umask(0o077)
        try:
            with self._open() as connection:
                connection.executescript(SCHEMA)
                _add_missing_columns(connection)
        finally:
            os.umask(previous)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht.

        `with sqlite3.connect(...)` ist ein Transaktionskontext und schliesst
        NICHTS — dieselbe Lehre wie im Tresor und im Hintergrundspeicher.
        """
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def permissions_ok(self) -> bool:
        try:
            mode = os.stat(self.path).st_mode & 0o777
        except OSError:
            return False
        return not (mode & 0o077)

    # -- Zahlungsmittel ------------------------------------------------------
    def put_instrument(self, instrument: Instrument) -> None:
        """Legt an oder ersetzt. Die Pruefsumme der Befugnis wandert mit.

        Sie ist der Grund, warum ein direkter Eingriff in die Datenbank nichts
        einbringt: der Executor rechnet sie aus der Zeile nach, und weicht sie
        ab, wird gar nicht erst bezahlt.
        """
        from solvio.payment.firewall import refuse_if_payment_material
        refuse_if_payment_material(instrument.display_name, where="instrument display name")
        refuse_if_payment_material(instrument.disabled_reason, where="instrument reason")
        row = instrument.authority_fields()
        with self._open() as connection:
            connection.execute(
                """INSERT INTO instruments (payment_ref, kind, provider, status, version,
                       max_single_minor, daily_total_minor, allowed_currencies,
                       allowed_merchant_ids, provider_secret_ref,
                       provider_readonly_ref, provider_refund_ref, provider_token,
                       display_name, display_hint, created_at, last_used_at,
                       disabled_reason, policy_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(payment_ref) DO UPDATE SET
                       kind=excluded.kind, provider=excluded.provider,
                       status=excluded.status, version=excluded.version,
                       max_single_minor=excluded.max_single_minor,
                       daily_total_minor=excluded.daily_total_minor,
                       allowed_currencies=excluded.allowed_currencies,
                       allowed_merchant_ids=excluded.allowed_merchant_ids,
                       provider_secret_ref=excluded.provider_secret_ref,
                       provider_readonly_ref=excluded.provider_readonly_ref,
                       provider_refund_ref=excluded.provider_refund_ref,
                       provider_token=excluded.provider_token,
                       display_name=excluded.display_name,
                       display_hint=excluded.display_hint,
                       disabled_reason=excluded.disabled_reason,
                       policy_sha256=excluded.policy_sha256""",
                (instrument.payment_ref, instrument.kind.value, instrument.provider,
                 instrument.status.value, int(instrument.version),
                 int(instrument.max_single_minor), int(instrument.daily_total_minor),
                 _join(instrument.allowed_currencies),
                 _join(instrument.allowed_merchant_ids),
                 instrument.provider_secret_ref, instrument.provider_readonly_ref,
                 instrument.provider_refund_ref, instrument.provider_token,
                 instrument.display_name, instrument.display_hint, instrument.created_at or utcnow_iso(),
                 instrument.last_used_at, instrument.disabled_reason,
                 instrument.digest()))

    def instrument(self, payment_ref: str) -> Instrument | None:
        """Liest ein Zahlungsmittel. Eine verfaelschte Zeile kommt NICHT zurueck.

        Die gespeicherte Pruefsumme wird gegen die aus der Zeile gerechnete
        gehalten. Wer in der Datenbank an einer Grenze dreht, bekommt kein
        hoeheres Limit, sondern ein `None` — und der Aufrufer bezahlt nicht.
        """
        try:
            ref = str(PR.parse(payment_ref))
        except PR.InvalidPaymentRef:
            return None
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM instruments WHERE payment_ref = ?", (ref,)).fetchone()
        if row is None:
            return None
        instrument = _instrument_from_row(row)
        if instrument.digest() != row["policy_sha256"]:
            log.error("payment.instrument_tampered", payment_ref=ref)
            return None
        return instrument

    def instruments(self) -> list[Instrument]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT payment_ref FROM instruments ORDER BY payment_ref").fetchall()
        out = []
        for row in rows:
            found = self.instrument(row["payment_ref"])
            if found is not None:
                out.append(found)
        return out

    def touch_instrument(self, payment_ref: str) -> None:
        with self._open() as connection:
            connection.execute(
                "UPDATE instruments SET last_used_at = ? WHERE payment_ref = ?",
                (utcnow_iso(), str(payment_ref)))

    def delete_instrument(self, payment_ref: str) -> bool:
        with self._open() as connection:
            cursor = connection.execute(
                "DELETE FROM instruments WHERE payment_ref = ?", (str(payment_ref),))
            return cursor.rowcount > 0

    # -- Absichten -----------------------------------------------------------
    def put_intent(self, intent: PaymentIntent) -> None:
        """Legt an oder schreibt FORT — nie um.

        Beim Fortschreiben aendert sich ausschliesslich, was sich aendern DARF:
        Zustand, Kostenvoranschlag, Ausfuehrungs- und Freigabekennung. Haendler,
        Herkunft, Waehrung, Positionen, Zahlungsmittel und Frist stehen
        ausdruecklich NICHT in der `DO UPDATE`-Liste — eine Absicht ist ab
        Anlage unveraenderlich, und das steht hier als SQL und nicht als
        Vorsatz.
        """
        from solvio.payment.firewall import refuse_if_payment_material
        refuse_if_payment_material(intent.purpose, where="intent purpose")
        for item in intent.items:
            refuse_if_payment_material(item.description, where="line item")
        with self._open() as connection:
            connection.execute(
                """INSERT INTO intents (payment_intent_id, requested_by, origin,
                       merchant_id, merchant_origin, currency, payment_method_ref,
                       purpose, items_json, created_at, expires_at,
                       agent_expected_total_minor, shipping_label,
                       shipping_destination_sha256, recurring, state, quote_json,
                       execution_id, approval_id, agent_run_id, task_id, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(payment_intent_id) DO UPDATE SET
                       state=excluded.state, quote_json=excluded.quote_json,
                       execution_id=excluded.execution_id,
                       approval_id=excluded.approval_id,
                       agent_run_id=excluded.agent_run_id,
                       task_id=excluded.task_id, updated_at=excluded.updated_at""",
                (intent.payment_intent_id, intent.requested_by, intent.origin,
                 intent.merchant_id, intent.merchant_origin, intent.currency,
                 intent.payment_method_ref, intent.purpose,
                 json.dumps([i.as_dict() for i in intent.items], ensure_ascii=False),
                 float(intent.created_at), float(intent.expires_at),
                 int(intent.agent_expected_total_minor), intent.shipping_label,
                 intent.shipping_destination_sha256, int(bool(intent.recurring)),
                 intent.state.value,
                 json.dumps(intent.quote.as_dict(), ensure_ascii=False)
                 if intent.quote else "",
                 intent.execution_id, intent.approval_id, intent.agent_run_id,
                 intent.task_id, utcnow_iso()))

    def intent(self, payment_intent_id: str) -> PaymentIntent | None:
        """Liest eine Absicht. Eine Zeile, die keine Absicht ERGIBT, kommt nicht zurueck.

        Die wirtschaftliche Identitaet einer Absicht — Haendler, Herkunft,
        Waehrung, Positionen, Zahlungsmittel — wird beim Schreiben NICHT
        aktualisiert (siehe `put_intent`): sie ist ab Anlage unveraenderlich.
        Wer trotzdem daran dreht, hinterlaesst eine Zeile, die sich nicht mehr
        zu einem gueltigen Objekt zusammensetzen laesst.

        Und dann ist die richtige Antwort `None`, nicht eine Ausnahme. Der
        Grund ist gemessen: eine Ausnahme aus dieser Funktion liefe im
        eingefrorenen Ausfuehrungspfad als „unbekannter Ausgang" auf, und der
        Mensch bekaeme „ich weiss nicht sicher, ob das durchging" fuer einen
        Aufruf, der den Rechner nie verlassen hat. Ein `None` fuehrt dagegen
        zur ehrlichen Absage „diesen Vorgang kenne ich nicht".
        """
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE payment_intent_id = ?",
                (str(payment_intent_id),)).fetchone()
        if row is None:
            return None
        try:
            return _intent_from_row(row)
        except (PaymentIntentError, ValueError, KeyError, TypeError) as exc:
            log.error("payment.intent_unreadable",
                      payment_intent_id=str(payment_intent_id)[:32],
                      kind=type(exc).__name__)
            return None

    def intents(self, *, states: Iterable[PaymentState] = (),
                limit: int = 50) -> list[PaymentIntent]:
        query = "SELECT * FROM intents"
        params: list[Any] = []
        wanted = [s.value for s in states]
        if wanted:
            query += " WHERE state IN (" + ",".join("?" * len(wanted)) + ")"
            params.extend(wanted)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._open() as connection:
            rows = connection.execute(query, params).fetchall()
        out: list[PaymentIntent] = []
        for row in rows:
            try:
                out.append(_intent_from_row(row))
            except (PaymentIntentError, ValueError, KeyError, TypeError):
                log.error("payment.intent_unreadable",
                          payment_intent_id=str(row["payment_intent_id"])[:32])
        return out

    # -- Buch ----------------------------------------------------------------
    def record(self, *, payment_intent_id: str, event: str, **fields: Any) -> None:
        """Eine Zeile ins Zahlungsbuch. Nie ein Wert, nie Zahlungsmaterial."""
        if event not in ALL_EVENTS:
            raise PaymentStoreError(f"unknown ledger event: {event[:32]}")
        from solvio.payment.firewall import refuse_if_payment_material
        refuse_if_payment_material(str(fields.get("description", "")),
                                   where="ledger description")
        refuse_if_payment_material(str(fields.get("provider_ref", "")),
                                   where="ledger provider ref")
        payload = {
            "at": utcnow_iso(), "payment_intent_id": str(payment_intent_id),
            "event": event,
            "merchant_id": str(fields.get("merchant_id", "")),
            "merchant_origin": str(fields.get("merchant_origin", "")),
            "description": str(fields.get("description", ""))[:400],
            "amount_minor": int(fields.get("amount_minor", 0) or 0),
            "currency": str(fields.get("currency", "")),
            "instrument_ref": str(fields.get("instrument_ref", "")),
            "origin": str(fields.get("origin", "")),
            "approval_id": str(fields.get("approval_id", "")),
            "execution_id": str(fields.get("execution_id", "")),
            "provider": str(fields.get("provider", "")),
            "provider_ref": str(fields.get("provider_ref", ""))[:128],
            "status": str(fields.get("status", "")),
            "refund_status": str(fields.get("refund_status", "")),
            "refunded_minor": int(fields.get("refunded_minor", 0) or 0),
            "failure_category": str(fields.get("failure_category", "")),
            "agent_run_id": str(fields.get("agent_run_id", "")),
            "task_id": str(fields.get("task_id", "")),
        }
        columns = ",".join(payload)
        marks = ",".join("?" * len(payload))
        try:
            with self._open() as connection:
                connection.execute(
                    f"INSERT INTO payment_ledger ({columns}) VALUES ({marks})",
                    list(payload.values()))
        except sqlite3.IntegrityError as exc:
            if event == EVENT_CHARGED:
                raise DuplicateCharge(
                    "a charge for this execution id is already booked") from exc
            raise

    def ledger(self, *, payment_intent_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM payment_ledger"
        params: list[Any] = []
        if payment_intent_id:
            query += " WHERE payment_intent_id = ?"
            params.append(str(payment_intent_id))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._open() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def charged_execution_ids(self) -> set[str]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT execution_id FROM payment_ledger "
                "WHERE event = ? AND execution_id <> ''", (EVENT_CHARGED,)).fetchall()
        return {row["execution_id"] for row in rows}

    def day_total_minor(self, payment_ref: str, *, day: str = "",
                        currency: str = "") -> int:
        """Was ueber dieses Mittel an DIESEM UTC-Tag schon belastet wurde.

        Aus dem Buch, nicht aus einem Zaehler: ein Neustart darf keine Grenze
        zurueckdrehen. Rueckerstattungen werden bewusst NICHT abgezogen — eine
        Tagesgrenze begrenzt, wie viel Geld an einem Tag BEWEGT werden darf.
        """
        prefix = (day or utcnow_iso())[:10]
        query = ("SELECT COALESCE(SUM(amount_minor), 0) AS total FROM payment_ledger "
                 "WHERE event = ? AND instrument_ref = ? AND substr(at, 1, 10) = ?")
        params: list[Any] = [EVENT_CHARGED, str(payment_ref), prefix]
        if currency:
            query += " AND currency = ?"
            params.append(str(currency))
        with self._open() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["total"] or 0)

    def open_reconciliations(self) -> list[dict[str, Any]]:
        """Was auf einen Menschen wartet. `EXECUTING` steht mit dabei.

        Ein Vorgang, der zwischen Anspruch und Antwort stehen blieb — Absturz,
        Stromausfall, `kill -9` —, ist NICHT erledigt. Er ist der gefaehrlichste
        Zustand ueberhaupt, weil die Belastung stattgefunden haben kann. Der
        erste Bau hat ihn nirgends gelesen; gefunden in der kalten Abnahme.

        Die frueheren `JOIN` auf `payment_ledger` warf ausserdem
        `ambiguous column name` und liess `payment_admin.py status` abstuerzen.
        """
        with self._open() as connection:
            rows = connection.execute(
                "SELECT i.payment_intent_id AS payment_intent_id, "
                "       i.merchant_id AS merchant_id, i.state AS state, "
                "       i.execution_id AS execution_id, i.updated_at AS at "
                "FROM intents AS i WHERE i.state IN (?,?,?) "
                "ORDER BY i.created_at DESC",
                (PaymentState.RECONCILIATION_REQUIRED.value,
                 PaymentState.AWAITING_SCA.value,
                 PaymentState.EXECUTING.value)).fetchall()
        return [dict(row) for row in rows]


# -- Zeilen zurueck in Objekte ----------------------------------------------
def _instrument_from_row(row: sqlite3.Row) -> Instrument:
    return Instrument(
        payment_ref=row["payment_ref"], kind=InstrumentKind(row["kind"]),
        provider=row["provider"], status=InstrumentStatus(row["status"]),
        max_single_minor=int(row["max_single_minor"]),
        daily_total_minor=int(row["daily_total_minor"]),
        allowed_currencies=_split(row["allowed_currencies"]),
        allowed_merchant_ids=_split(row["allowed_merchant_ids"]),
        provider_secret_ref=row["provider_secret_ref"],
        provider_readonly_ref=row["provider_readonly_ref"],
        provider_refund_ref=row["provider_refund_ref"],
        provider_token=row["provider_token"], version=int(row["version"]),
        display_name=row["display_name"], display_hint=row["display_hint"],
        created_at=row["created_at"], last_used_at=row["last_used_at"],
        disabled_reason=row["disabled_reason"])


def _intent_from_row(row: sqlite3.Row) -> PaymentIntent:
    items = tuple(
        LineItem(description=entry["description"], quantity=int(entry["quantity"]),
                 unit_amount_minor=int(entry["unit_amount_minor"]),
                 item_id=entry.get("item_id", ""))
        for entry in json.loads(row["items_json"]))
    quote = None
    if row["quote_json"]:
        raw = json.loads(row["quote_json"])
        quote = PaymentQuote(
            source=raw["source"], currency=raw["currency"],
            items_total_minor=int(raw["items_total_minor"]),
            extras=tuple(Extra(kind=ExtraKind(e["kind"]),
                               amount_minor=int(e["amount_minor"]),
                               label=e.get("label", ""))
                         for e in raw.get("extras", [])),
            total_minor=int(raw["total_minor"]), version=int(raw.get("version", 1)),
            quoted_at=float(raw.get("quoted_at", 0.0)),
            quote_ref=raw.get("quote_ref", ""),
            charged_currency=raw.get("charged_currency", ""),
            charged_total_minor=int(raw.get("charged_total_minor", 0)))
    return PaymentIntent(
        payment_intent_id=row["payment_intent_id"], requested_by=row["requested_by"],
        origin=row["origin"], merchant_id=row["merchant_id"],
        merchant_origin=row["merchant_origin"], currency=row["currency"],
        payment_method_ref=row["payment_method_ref"], purpose=row["purpose"],
        items=items, created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        agent_expected_total_minor=int(row["agent_expected_total_minor"]),
        shipping_label=row["shipping_label"],
        shipping_destination_sha256=row["shipping_destination_sha256"],
        recurring=bool(row["recurring"]), state=PaymentState(row["state"]),
        quote=quote, execution_id=row["execution_id"],
        approval_id=row["approval_id"], agent_run_id=row["agent_run_id"],
        task_id=row["task_id"])
