"""Dauerhafte Auftragskosten im VORHANDENEN Agentenbuch.

Dies ist eine Core-interne Buchungsnaht, kein Werkzeug und kein neuer Speicher.
Der Aufrufer muss Belege, Auftragszuordnung und Nutzerentscheidungen aus seinen
vertrauenswuerdigen Diensten liefern. Eine Modellaussage ist kein Kostenbeleg.
Insbesondere beweist eine Abo-Anmeldung weder ausgeschaltete Extra Usage noch
kostenlose Arbeit. Ohne belegte Kostenfreiheit oder technisch durchsetzbare
Obergrenze wird vor dem Anbieteraufruf gehalten. Ein separat mit Face ID
freigegebener nativer Credit-Aufruf ist seit dem Owner-Entscheid 01.10.2026
eine ausdrueckliche Ausnahme: keine gemessene EUR-Obergrenze oder Abrechnung.
Diese Zeilen haben upper/actual NULL; Anzeige und Beleg benennen die Luecke.

Reservieren beansprucht Geld, nicht die Ausfuehrung: deren Einmaligkeit bleibt
im bestehenden Ausfuehrungsjournal. Auch ein wiederholter Reservierungsaufruf
berechtigt nicht zur Wiederholung einer externen Handlung. Ein unbekannter
Ausgang bleibt belastet, bis echte Abrechnung oder ein Nichtversandbeleg vorliegt.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass

from solvio.agent_runtime import store as S

DEFAULT_THRESHOLD_CENTS = 1000
MAX_CENTS = 1_000_000_000
CATEGORIES = frozenset({"ai_tool", "purchase"})
EVIDENCE_KINDS = frozenset({
    "enforceable_upper_bound", "included_no_extra_charge", "free_local",
    "actual_charge", "not_dispatched", "unknown", "subscription_auth",
    "owner_authorized_credits",
})
ZERO_COST_EVIDENCE = frozenset({"included_no_extra_charge", "free_local"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_cost_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    ask_threshold_cents INTEGER NOT NULL CHECK(ask_threshold_cents >= 0),
    authority_ref TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_cost_settings_decisions (
    authority_ref TEXT PRIMARY KEY,
    ask_threshold_cents INTEGER NOT NULL CHECK(ask_threshold_cents >= 0),
    decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_cost_policies (
    task_id TEXT PRIMARY KEY REFERENCES agent_tasks(task_id),
    ask_threshold_cents INTEGER NOT NULL CHECK(ask_threshold_cents >= 0),
    approved_ai_cap_cents INTEGER,
    purchase_cap_cents INTEGER,
    authority_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_cost_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES agent_cost_policies(task_id),
    invocation_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('ai_tool', 'purchase')),
    route TEXT NOT NULL,
    upper_bound_cents INTEGER,
    evidence TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('reserved','approval_required','unbounded_cost','unknown','settled','released')),
    actual_cents INTEGER,
    settlement_evidence TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(task_id, invocation_id)
);
CREATE INDEX IF NOT EXISTS agent_cost_task ON agent_cost_reservations(task_id);
CREATE TABLE IF NOT EXISTS agent_cost_authorizations (
    task_id TEXT NOT NULL REFERENCES agent_cost_policies(task_id),
    approval_ref TEXT NOT NULL,
    category TEXT NOT NULL,
    max_total_cents INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(approval_ref)
);
"""


def _cents(value: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CENTS:
        raise ValueError(f"invalid_{field}")
    return value


def _text(value: str, field: str, limit: int = 256, *, empty=False) -> str:
    if (not isinstance(value, str) or len(value) > limit or (not value and not empty)
            or (value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]*", value))):
        raise ValueError(f"invalid_{field}")
    return S._safe_text(value, limit, where=f"agent_cost.{field}")


@dataclass(frozen=True)
class CostEvidence:
    """Gemessener/Core-gepruefter Belegverweis; nie der freie Anbietertext.

    ``included_no_extra_charge`` verlangt einen Nachweis, der zusaetzliche
    Belastung ausschliesst, nicht nur einen Loginstatus. ``enforceable_upper_bound``
    verlangt einen tatsaechlich begrenzten Aufruf. Eine reine Schaetzung reicht
    nicht. Die Pruefung dieser Sachverhalte besitzt der jeweilige Adapter.
    """

    kind: str
    reference: str = ""
    currency: str = "EUR"

    def __post_init__(self):
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError("invalid_cost_evidence_kind")
        _text(self.reference, "evidence", 512, empty=True)
        if self.currency != "EUR":
            raise ValueError("cost_evidence_requires_eur")

    def json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CostDecision:
    status: str
    reason: str = ""
    reservation_id: str = ""
    task_id: str = ""
    projected_cents: int = 0
    replayed: bool = False
    subject_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.status == "reserved"


class CostLedger:
    def __init__(self, ledger: S.AgentRunLedger, *,
                 default_threshold_cents: int = DEFAULT_THRESHOLD_CENTS):
        self.ledger = ledger
        self.default_threshold_cents = _cents(default_threshold_cents, "threshold")
        # Dieselbe Datei, dieselben Rechte, dieselbe FULL/WAL-Verbindung.
        with ledger._open() as connection:
            from solvio.agent_runtime.cost_subjects import ensure_schema
            ensure_schema(ledger, SCHEMA)
            connection.execute("INSERT OR IGNORE INTO agent_cost_settings VALUES (1,?,?,?)",
                (self.default_threshold_cents, "core:initial_configuration", time.time()))
            # Bei einer bereits vorhandenen N2-Ablage wenigstens die aktuelle
            # Entscheidung binden; alte, nie gespeicherte Historie nicht erfinden.
            connection.execute("INSERT OR IGNORE INTO agent_cost_settings_decisions "
                "SELECT authority_ref,ask_threshold_cents,updated_at FROM agent_cost_settings WHERE singleton=1")

    def _decision(self, status, reason="", reservation_id="", subject_id="",
                  projected_cents=0, replayed=False):
        task_id = ""
        if subject_id:
            with self.ledger._open() as db:
                row = db.execute("SELECT task_id FROM agent_cost_subjects WHERE subject_id=?", (subject_id,)).fetchone()
                task_id = (row[0] or "") if row else ""
                if not row and db.execute("SELECT 1 FROM agent_tasks WHERE task_id=?", (subject_id,)).fetchone():
                    task_id = subject_id
        return CostDecision(status, reason, reservation_id, task_id, projected_cents, replayed, subject_id)

    def subject(self, subject_id):
        from solvio.agent_runtime.cost_subjects import CostSubject
        _text(subject_id, "subject_id")
        with self.ledger._open() as db:
            row = db.execute("SELECT subject_id,kind,task_id,principal FROM agent_cost_subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not row:
                raise ValueError("unknown_cost_subject")
            return CostSubject(**dict(row))

    def _task(self, task_id):
        _text(task_id, "task_id")
        with self.ledger._open() as db:
            if not db.execute("SELECT 1 FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("unknown_task")
        return task_id

    def configure(self, task_id, **kwargs):
        self._configure_task(self._task(task_id), **kwargs)
        return self.view(task_id)

    def reserve(self, task_id, invocation_id, upper_bound_cents, **kwargs):
        return self.reserve_subject(self._task(task_id), invocation_id, upper_bound_cents, **kwargs)

    def approve(self, task_id, **kwargs):
        self.approve_subject(self._task(task_id), **kwargs)
        return self.view(task_id)

    def view(self, task_id):
        # Preserve the N2 public projection, including unconfigured task reads.
        result = self.view_subject(task_id)
        return {key: value for key, value in result.items() if key not in ("subject_id", "subject_kind")} | {"task_id": task_id}

    def settings(self) -> dict:
        with self.ledger._open() as connection:
            return dict(connection.execute("SELECT ask_threshold_cents,authority_ref,updated_at "
                                           "FROM agent_cost_settings WHERE singleton=1").fetchone())

    def set_default_threshold(self, cents: int, authority_ref: str) -> dict:
        """Gepruefte Owner-Konfiguration; Retry einer alten Entscheidung ist lesend.

        A(1500), B(500), spaeter A darf B nicht zurueckdrehen. Deshalb bindet
        die Kennung dauerhaft ihren Wert, und nur eine NEUE Entscheidung
        schreibt den aktuellen Stand. Bestehende Auftraege bleiben unveraendert.
        """
        _cents(cents, "threshold")
        authority_ref = _text(authority_ref, "authority_ref")
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT ask_threshold_cents FROM agent_cost_settings_decisions "
                                       "WHERE authority_ref=?", (authority_ref,)).fetchone()
            if prior:
                if prior["ask_threshold_cents"] != cents:
                    raise ValueError("cost_settings_binding_mismatch")
            else:
                now = time.time()
                connection.execute("INSERT INTO agent_cost_settings_decisions VALUES (?,?,?)",
                                   (authority_ref, cents, now))
                connection.execute("UPDATE agent_cost_settings SET ask_threshold_cents=?,"
                    "authority_ref=?,updated_at=? WHERE singleton=1", (cents, authority_ref, now))
        return self.settings()

    def _configure_task(self, subject_id: str, *, ask_threshold_cents: int | None = None,
                  purchase_cap_cents: int | None = None,
                  authority_ref: str = "") -> dict:
        """Policy genau einmal am Auftrag festhalten; kein stilles Anheben.

        Der globale Startwert wird hier kopiert und aendert bestehende Auftraege
        spaeter nicht. Ein Kaufbudget braucht einen expliziten Owner-Auftragsbeleg.
        """
        subject_id = _text(subject_id, "subject_id")
        if ask_threshold_cents is not None:
            _cents(ask_threshold_cents, "threshold")
        authority_ref = _text(authority_ref, "authority_ref", empty=True)
        if purchase_cap_cents is not None:
            _cents(purchase_cap_cents, "purchase_cap")
            if not authority_ref:
                raise ValueError("purchase_authority_required")
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM agent_cost_policies WHERE subject_id=?",
                                       (subject_id,)).fetchone()
            if prior:
                if ((ask_threshold_cents is not None and
                     prior["ask_threshold_cents"] != ask_threshold_cents) or
                    (purchase_cap_cents is not None and
                     prior["purchase_cap_cents"] != purchase_cap_cents) or
                    (authority_ref and prior["authority_ref"] != authority_ref)):
                    raise ValueError("cost_policy_already_bound")
            else:
                if not connection.execute("SELECT created_principal FROM agent_tasks WHERE task_id=?",
                                          (subject_id,)).fetchone():
                    raise ValueError("unknown_task")
                task = connection.execute("SELECT created_principal FROM agent_tasks WHERE task_id=?", (subject_id,)).fetchone()
                connection.execute("INSERT OR IGNORE INTO agent_cost_subjects "
                    "(subject_id,kind,task_id,principal,source_kind,conversation_id,created_at) "
                    "VALUES (?,'task',?,?,'task',?,?)",
                    (subject_id, subject_id, task["created_principal"], subject_id, time.time()))
                threshold = (ask_threshold_cents if ask_threshold_cents is not None else
                    connection.execute("SELECT ask_threshold_cents FROM agent_cost_settings "
                                       "WHERE singleton=1").fetchone()[0])
                connection.execute("INSERT INTO agent_cost_policies "
                    "(subject_id,ask_threshold_cents,purchase_cap_cents,authority_ref,created_at) "
                    "VALUES (?,?,?,?,?)", (subject_id, threshold, purchase_cap_cents,
                                          authority_ref, time.time()))
        return self.view_subject(subject_id)

    @staticmethod
    def _totals(connection, subject_id: str) -> dict:
        totals = {category: {"spent_cents": 0, "reserved_cents": 0,
                             "total_cents": 0, "overrun": False}
                  for category in CATEGORIES}
        rows = connection.execute("SELECT * FROM agent_cost_reservations WHERE subject_id=?",
                                  (subject_id,)).fetchall()
        for row in rows:
            item = totals[row["category"]]
            credit = json.loads(row["evidence"]).get("kind") == "owner_authorized_credits"
            if credit and row["state"] in ("reserved", "unknown", "settled"):
                item["credit_usage_unmeasured"] = item.get("credit_usage_unmeasured", 0) + 1
                # Credits have no measured EUR conversion. Do not pretend that
                # their missing amount is a zero-cost/free subscription call.
                continue
            if row["state"] == "settled":
                item["spent_cents"] += row["actual_cents"]
                item["overrun"] |= row["actual_cents"] > row["upper_bound_cents"]
            elif row["state"] in ("reserved", "unknown"):
                item["reserved_cents"] += row["upper_bound_cents"]
        for item in totals.values():
            item["total_cents"] = item["spent_cents"] + item["reserved_cents"]
        return totals

    def reserve_subject(self, subject_id: str, invocation_id: str, upper_bound_cents: int | None,
                *, category: str = "ai_tool", route: str,
                evidence: CostEvidence) -> CostDecision:
        """Atomar VOR dem Dispatch. Dieselbe Invocation hat dieselbe Bindung.

        Bei fehlendem Beleg bleibt auch eine angegebene Null unbounded_cost.
        Nach Kostenfreigabe kann dieselbe wartende Invocation reserviert werden;
        ihre Route, Kategorie und Obergrenze bleiben unveraenderlich.
        """
        subject_id = _text(subject_id, "subject_id")
        invocation_id = _text(invocation_id, "invocation_id")
        route = _text(route, "route")
        if category not in CATEGORIES:
            raise ValueError("invalid_cost_category")
        if not isinstance(evidence, CostEvidence):
            raise ValueError("cost_evidence_required")
        if upper_bound_cents is not None:
            _cents(upper_bound_cents, "upper_bound")
        encoded = evidence.json()
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            policy = connection.execute("SELECT * FROM agent_cost_policies WHERE subject_id=?",
                                        (subject_id,)).fetchone()
            if not policy:
                return self._decision("refused", "cost_policy_missing", subject_id=subject_id)
            prior = connection.execute("SELECT * FROM agent_cost_reservations "
                "WHERE subject_id=? AND invocation_id=?", (subject_id, invocation_id)).fetchone()
            if prior and (prior["category"], prior["route"], prior["upper_bound_cents"],
                          prior["evidence"]) != (category, route, upper_bound_cents, encoded):
                return self._decision("refused", "invocation_binding_mismatch",
                                    prior["reservation_id"], subject_id, replayed=True)
            totals = self._totals(connection, subject_id)
            if any(item["overrun"] for item in totals.values()):
                return self._decision("refused", "cost_bound_exceeded",
                    prior["reservation_id"] if prior else "", subject_id,
                    totals[category]["total_cents"], bool(prior))
            if prior and prior["state"] in ("reserved", "unknown", "settled", "released"):
                status = "reserved" if prior["state"] == "reserved" else "refused"
                return self._decision(status, "invocation_" + prior["state"],
                    prior["reservation_id"], subject_id, totals[category]["total_cents"], True)
            bounded = (bool(evidence.reference) and upper_bound_cents is not None and
                ((upper_bound_cents == 0 and evidence.kind in ZERO_COST_EVIDENCE) or
                 (upper_bound_cents > 0 and evidence.kind == "enforceable_upper_bound")))
            credit = (category == "ai_tool" and route == "codex" and upper_bound_cents is None
                      and evidence.kind == "owner_authorized_credits"
                      and evidence.reference.startswith("native-credit:approval:"))
            projected = totals[category]["total_cents"] + (upper_bound_cents or 0)
            if credit:
                # Separate authority supplied by the Core's native adapter;
                # neither the EUR threshold nor a purchase budget authorizes it.
                status, reason = "reserved", "owner_authorized_credit_usage"
            elif not bounded:
                status, reason = "unbounded_cost", "cost_bound_unproven"
            elif category == "purchase":
                allowed = (policy["purchase_cap_cents"] is not None and
                           projected <= policy["purchase_cap_cents"])
                status, reason = (("reserved", "") if allowed else
                                  ("approval_required", "purchase_budget_required"))
            else:
                allowed = (projected < policy["ask_threshold_cents"] or
                    (policy["approved_ai_cap_cents"] is not None and
                     projected <= policy["approved_ai_cap_cents"]))
                # Ein nachgewiesen kostenloser Aufruf verbraucht auch bei einer
                # konfigurierten Nullschwelle keine zusaetzlichen Mittel.
                allowed = allowed or (upper_bound_cents == 0 and projected == 0)
                status, reason = (("reserved", "") if allowed else
                                  ("approval_required", "ai_tool_budget_required"))
            reservation_id = prior["reservation_id"] if prior else "ac-" + secrets.token_hex(8)
            now = time.time()
            if prior:
                connection.execute("UPDATE agent_cost_reservations SET state=?,updated_at=? "
                    "WHERE reservation_id=?", (status, now, reservation_id))
            else:
                connection.execute("INSERT INTO agent_cost_reservations "
                    "(reservation_id,subject_id,invocation_id,category,route,upper_bound_cents,"
                    "evidence,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (reservation_id, subject_id, invocation_id, category, route,
                     upper_bound_cents, encoded, status, now, now))
            return self._decision(status, reason, reservation_id, subject_id, projected, bool(prior))

    def approve_subject(self, subject_id: str, *, max_total_cents: int, approval_ref: str,
                category: str = "ai_tool") -> dict:
        """Nur der authentifizierte Owner-Dienst ruft dies, niemals ein Modell.

        Ein explizit freigegebener Gesamtdeckel ist INKLUSIV: 20 EUR erlauben
        20 EUR. Die urspruengliche Rueckfrageschwelle bleibt als Beleg erhalten.
        Diese Methode fuehrt keine Zahlung/Anbieterroute ein und startet nichts.
        """
        _cents(max_total_cents, "approved_cap")
        subject_id = _text(subject_id, "subject_id")
        approval_ref = _text(approval_ref, "approval_ref")
        if category not in CATEGORIES:
            raise ValueError("invalid_cost_category")
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            policy = connection.execute("SELECT * FROM agent_cost_policies WHERE subject_id=?",
                                        (subject_id,)).fetchone()
            if not policy:
                raise ValueError("cost_policy_missing")
            prior = connection.execute("SELECT * FROM agent_cost_authorizations "
                "WHERE approval_ref=?", (approval_ref,)).fetchone()
            if prior:
                if (prior["subject_id"], prior["category"], prior["max_total_cents"]) != (
                        subject_id, category, max_total_cents):
                    raise ValueError("cost_approval_binding_mismatch")
            else:
                column = "approved_ai_cap_cents" if category == "ai_tool" else "purchase_cap_cents"
                current = policy[column]
                if current is not None and max_total_cents < current:
                    raise ValueError("approval_cannot_lower_existing_cap")
                connection.execute(f"UPDATE agent_cost_policies SET {column}=? WHERE subject_id=?",
                                   (max_total_cents, subject_id))
                connection.execute("INSERT INTO agent_cost_authorizations VALUES (?,?,?,?,?)",
                    (subject_id, approval_ref, category, max_total_cents, time.time()))
        return self.view_subject(subject_id)

    def settle(self, reservation_id: str, actual_cents: int | None,
               evidence: CostEvidence) -> CostDecision:
        """Echte Kosten ersetzen die Reserve, auch wenn die Obergrenze versagt.

        Eine solche Ueberschreitung wird ehrlich gebucht und haelt weitere
        Aufrufe an. Sie wird niemals durch Abschneiden der Abrechnung versteckt.
        """
        credit = (isinstance(evidence, CostEvidence)
                  and evidence.kind == "owner_authorized_credits" and actual_cents is None)
        if not credit:
            _cents(actual_cents, "actual_cost")
        reservation_id = _text(reservation_id, "reservation_id")
        if (not isinstance(evidence, CostEvidence) or not evidence.reference or
            (not credit and evidence.kind not in ({"actual_charge"} | ZERO_COST_EVIDENCE)) or
            (actual_cents != 0 and evidence.kind in ZERO_COST_EVIDENCE)):
            raise ValueError("actual_cost_evidence_required")
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM agent_cost_reservations WHERE reservation_id=?",
                                     (reservation_id,)).fetchone()
            if not row:
                return self._decision("refused", "unknown_reservation")
            if credit and (row["evidence"] != evidence.json() or row["upper_bound_cents"] is not None
                           or row["category"] != "ai_tool" or row["route"] != "codex"):
                return self._decision("refused", "credit_settlement_mismatch", reservation_id, row["subject_id"])
            if not credit and json.loads(row["evidence"]).get("kind") == "owner_authorized_credits":
                return self._decision("refused", "credit_settlement_mismatch", reservation_id, row["subject_id"])
            if row["state"] == "settled":
                reason = "already_settled" if row["actual_cents"] == actual_cents else "settlement_mismatch"
                return self._decision("settled" if reason == "already_settled" else "refused",
                                    reason, reservation_id, row["subject_id"], replayed=True)
            if row["state"] not in ("reserved", "unknown"):
                return self._decision("refused", "reservation_not_open", reservation_id, row["subject_id"])
            connection.execute("UPDATE agent_cost_reservations SET state='settled',actual_cents=?,"
                "settlement_evidence=?,updated_at=? WHERE reservation_id=?",
                (actual_cents, evidence.json(), time.time(), reservation_id))
            reason = "credit_usage_unmeasured" if credit else (
                "cost_bound_exceeded" if actual_cents > row["upper_bound_cents"] else "")
            return self._decision("settled", reason, reservation_id, row["subject_id"], actual_cents or 0)

    def mark_unknown(self, reservation_id: str) -> bool:
        """Ungewissheit ist keine Rueckbuchung und kein Wiederholungsrecht."""
        reservation_id = _text(reservation_id, "reservation_id")
        with self.ledger._open() as connection:
            cursor = connection.execute("UPDATE agent_cost_reservations SET state='unknown',updated_at=? "
                "WHERE reservation_id=? AND state='reserved'", (time.time(), reservation_id))
            return bool(cursor.rowcount)

    def release(self, reservation_id: str, evidence: CostEvidence) -> bool:
        """Nur belegter Nichtversand gibt eine Reserve frei, nie ein Timeout."""
        reservation_id = _text(reservation_id, "reservation_id")
        if (not isinstance(evidence, CostEvidence) or evidence.kind != "not_dispatched"
                or not evidence.reference):
            raise ValueError("not_dispatched_evidence_required")
        with self.ledger._open() as connection:
            cursor = connection.execute("UPDATE agent_cost_reservations SET state='released',"
                "settlement_evidence=?,updated_at=? WHERE reservation_id=? AND state IN ('reserved','unknown')",
                (evidence.json(), time.time(), reservation_id))
            return bool(cursor.rowcount)

    def view_subject(self, subject_id: str) -> dict:
        subject_id = _text(subject_id, "subject_id")
        with self.ledger._open() as connection:
            connection.execute("BEGIN")
            policy = connection.execute("SELECT * FROM agent_cost_policies WHERE subject_id=?",
                                        (subject_id,)).fetchone()
            if not policy:
                return {"subject_id": subject_id, "configured": False, "currency": "EUR"}
            states = connection.execute("SELECT state,COUNT(*) AS n FROM agent_cost_reservations "
                "WHERE subject_id=? GROUP BY state", (subject_id,)).fetchall()
            subject = connection.execute("SELECT kind,task_id FROM agent_cost_subjects WHERE subject_id=?", (subject_id,)).fetchone()
            totals = self._totals(connection, subject_id)
            for item in totals.values():
                if item.get("credit_usage_unmeasured"):
                    # Old clients already render null as 'not confirmed'. New
                    # clients can explain why using the additive credit flag.
                    item.update(spent_cents=None, reserved_cents=None, total_cents=None)
            return {"subject_id": subject_id, "subject_kind": subject["kind"], "task_id": subject["task_id"], "configured": True, "currency": "EUR",
                "ask_threshold_cents": policy["ask_threshold_cents"],
                "approved_ai_cap_cents": policy["approved_ai_cap_cents"],
                "purchase_cap_cents": policy["purchase_cap_cents"],
                "counts": {row["state"]: row["n"] for row in states},
                **totals}
