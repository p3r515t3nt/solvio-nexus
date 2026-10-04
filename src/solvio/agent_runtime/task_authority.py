"""Gebundene Aufgabenbefugnis im bestehenden Agentenledger.

Der Eingangsdienst verifiziert App-Sitzung, Browser-Sitzung oder bestehende
Face-ID-/Dashboard-Entscheidung, BEVOR er issue ruft. Dieses Modul authentifiziert
keine HTTP-Anfrage. VerifiedTaskReceipt ist ein Core-interner Vertrag ueber das
bereits gepruefte Ergebnis, kein Freitextbeweis und kein Modellwerkzeug.

Ein Grant ist die dauerhafte, enge Bindung dieser Entscheidung an genau eine
Aufgabe und einen Lauf. Die Freigabeentscheidung selbst bleibt in ihrem
bisherigen Journal. Die Referenz ist lediglich ein Zeiger: jeder Gebrauch liest
den aktuellen Auftrag, Widerruf und Laufzustand erneut. Hintergrundherkunft,
Provenienz und die Faehigkeitspolitik werden hier niemals herabgestuft.

Der Ausfuehrungsanspruch liegt am bestehenden agent_steps-Datensatz. Vor einem
Effekt muss claim_step unmittelbar nach den anderen Toren erfolgreich sein.
Eine unklare oder unterbrochene alte Wirkung wird dadurch nicht erneut gestartet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any

from solvio.agent_runtime import store as S

RECEIPT_METHODS = frozenset({"app_session", "dashboard_session", "face_id", "dashboard_ok"})
MAX_GRANT_JSON = 32_000
MAX_CAPABILITIES = 128
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_task_grants (
    reference TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    task_fingerprint TEXT NOT NULL,
    receipt_method TEXT NOT NULL,
    receipt_reference TEXT NOT NULL UNIQUE,
    authorizer TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    revocation_ref TEXT NOT NULL DEFAULT ''
);
"""


class GrantError(ValueError):
    pass


def _identifier(value: str, field_name: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]*", value)):
        raise GrantError("invalid_" + field_name)
    S._refuse_credentials(value, where="task_authority." + field_name)
    return value


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise GrantError("arguments_too_deep")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise GrantError("non_json_argument")


def _canonical(value: Any) -> str:
    _json_value(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    if len(encoded) > MAX_GRANT_JSON:
        raise GrantError("grant_payload_too_large")
    S._refuse_credentials(encoded, where="task_authority.binding")
    return encoded


def _digest(domain: str, value: Any) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + _canonical(value).encode("utf-8")).hexdigest()


def _version(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise GrantError("invalid_capability_version")
    return value


@dataclass(frozen=True)
class VerifiedTaskReceipt:
    """Vom authentifizierenden Core-Dienst geliefert, NICHT selbstverifizierend."""

    method: str
    reference: str
    authorizer: str

    def __post_init__(self):
        if self.method not in RECEIPT_METHODS:
            raise GrantError("invalid_receipt_method")
        _identifier(self.reference, "receipt_reference")
        _identifier(self.authorizer, "authorizer")


@dataclass(frozen=True)
class CapabilityGrant:
    name: str
    version: int
    #: Nur diese Felder sind gebunden; jedes davon muss EXAKT passen.
    #: Andere Felder unterliegen weiter dem Schema und der Router-Politik.
    constraints: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise GrantError("invalid_capability")
        _version(self.version)
        if type(self.constraints) is not dict:
            raise GrantError("constraints_must_be_object")
        _canonical(self.constraints)


@dataclass(frozen=True)
class TaskGrant:
    reference: str
    task_id: str
    run_id: str
    task_fingerprint: str
    receipt_method: str
    receipt_reference: str
    authorizer: str
    capabilities: tuple[CapabilityGrant, ...]
    created_at: float
    expires_at: float | None = None
    revoked_at: float | None = None


@dataclass(frozen=True)
class GrantDecision:
    allowed: bool
    reason: str = ""
    reference: str = ""
    binding_digest: str = ""


def _task_fingerprint(task) -> str:
    return _digest("SOLVIO_TASK_FINGERPRINT_V1", {
        key: task[key] for key in ("task_id", "objective", "scope", "target_repo",
                                  "created_principal", "created_origin")})


def _run_fingerprint(connection, task, run_id) -> str:
    """Original task binding, plus a verified Owner revision for newer runs."""
    from solvio.agent_runtime.task_revisions import grant_revision_digest
    original = _task_fingerprint(task)
    revision = grant_revision_digest(connection, task, run_id)
    if not revision:
        return original
    return _digest("SOLVIO_TASK_REVISION_FINGERPRINT_V1", {
        "original_task_fingerprint": original, "run_id": run_id,
        "revision_digest": revision})


def _grant_binding(*, task_id, run_id, fingerprint, method, receipt, authorizer,
                   capabilities, expires_at):
    return _digest("SOLVIO_TASK_GRANT_V1", {
        "task_id": task_id, "run_id": run_id, "task_fingerprint": fingerprint,
        "method": method, "receipt": receipt, "authorizer": authorizer,
        "capabilities": capabilities, "expires_at": expires_at})


def _from_row(row) -> TaskGrant:
    return TaskGrant(reference=row["reference"], task_id=row["task_id"], run_id=row["run_id"],
        task_fingerprint=row["task_fingerprint"], receipt_method=row["receipt_method"],
        receipt_reference=row["receipt_reference"], authorizer=row["authorizer"],
        capabilities=tuple(CapabilityGrant(**item) for item in json.loads(row["capabilities"])),
        created_at=row["created_at"], expires_at=row["expires_at"], revoked_at=row["revoked_at"])


class TaskAuthority:
    def __init__(self, ledger: S.AgentRunLedger, *, clock=time.time):
        self.ledger = ledger
        self.clock = clock
        with ledger._open() as connection:
            connection.executescript(SCHEMA)

    def issue(self, task_id: str, run_id: str, *, receipt: VerifiedTaskReceipt,
              capabilities: tuple[CapabilityGrant, ...], expires_at: float | None = None) -> TaskGrant:
        """Ein bereits autorisierter Auftrag; dieselbe Bindung ist idempotent.

        Ein Lauf erhaelt einen unveraenderlichen Grant. Weder ein spaeteres
        issue noch eine neue Modellplanung erweitert seine Faehigkeitenliste.
        """
        _identifier(task_id, "task_id")
        _identifier(run_id, "run_id")
        if type(receipt) is not VerifiedTaskReceipt:
            raise GrantError("verified_receipt_required")
        if not isinstance(capabilities, (tuple, list)) or len(capabilities) > MAX_CAPABILITIES:
            raise GrantError("invalid_capabilities")
        allowed = []
        seen = set()
        for entry in capabilities:
            if type(entry) is not CapabilityGrant:
                raise GrantError("typed_capability_grant_required")
            # Frozen dataclasses verhindern keine Mutation des enthaltenen
            # Dictionaries: noch einmal pruefen und in JSON selbst kopieren.
            CapabilityGrant(entry.name, entry.version, entry.constraints)
            if entry.name in seen:
                raise GrantError("duplicate_capability")
            seen.add(entry.name)
            allowed.append({"name": entry.name, "version": entry.version,
                            "constraints": entry.constraints})
        allowed.sort(key=lambda item: item["name"])
        encoded = _canonical(allowed)
        now = self.clock()
        if expires_at is not None:
            if (type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                    or expires_at <= now):
                raise GrantError("invalid_grant_expiry")
            expires_at = float(expires_at)
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not task or not run or run["task_id"] != task_id:
                raise GrantError("task_run_mismatch")
            if run["state"] in S.TERMINAL_STATES or run["finished_at"] is not None:
                raise GrantError("run_terminal")
            if task["state"] != S.TASK_ACTIVE:
                raise GrantError("task_inactive")
            from solvio.agent_runtime.action_intent import validate_issue
            validate_issue(connection, self.ledger, task_id, run_id, receipt, allowed)
            from solvio.agent_runtime.task_revisions import validate_source as validate_revision_source
            validate_revision_source(connection, task_id, run_id, receipt, allowed)
            from solvio.agent_runtime.file_inputs import validate_issue as validate_file_issue
            validate_file_issue(connection, task_id, run_id, receipt, allowed)
            action_row = connection.execute('SELECT * FROM agent_action_contracts WHERE run_id=?', (run_id,)).fetchone() \
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_action_contracts'").fetchone() else None
            if action_row is not None:
                from solvio.agent_runtime import action_contract as AC
                from solvio.agent_runtime.portal_connection import validate_row
                validate_row(connection, self.ledger, AC._bound_row(action_row), receipt, allowed)
            fingerprint = _run_fingerprint(connection, task, run_id)
            binding = _grant_binding(task_id=task_id, run_id=run_id, fingerprint=fingerprint,
                method=receipt.method, receipt=receipt.reference, authorizer=receipt.authorizer,
                capabilities=json.loads(encoded), expires_at=expires_at)
            prior = connection.execute("SELECT * FROM agent_task_grants "
                "WHERE receipt_reference=? OR run_id=?", (receipt.reference, run_id)).fetchall()
            if prior:
                if len(prior) != 1 or prior[0]["binding_digest"] != binding:
                    raise GrantError("grant_already_bound")
                if prior[0]["revoked_at"] is not None:
                    raise GrantError("grant_revoked")
                return _from_row(prior[0])
            reference = "ag-" + secrets.token_hex(8)
            connection.execute("INSERT INTO agent_task_grants "
                "(reference,task_id,run_id,task_fingerprint,receipt_method,receipt_reference,"
                "authorizer,capabilities,binding_digest,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (reference, task_id, run_id, fingerprint, receipt.method, receipt.reference,
                 receipt.authorizer, encoded, binding, now, expires_at))
            return _from_row(connection.execute("SELECT * FROM agent_task_grants WHERE reference=?",
                                               (reference,)).fetchone())

    def for_run(self, run_id: str) -> TaskGrant | None:
        """Nur ein Locator fuer den Core; aktiv wird der Grant erst durch verify."""
        _identifier(run_id, "run_id")
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_task_grants WHERE run_id=?", (run_id,)).fetchone()
            return _from_row(row) if row else None

    # Expliziter Name fuer den fortsetzenden Core-Dienst: auch diese Lesung
    # liefert lediglich den Locator, keinen bereits verifizierten Effekt.
    reference_for_run = for_run

    def revoke(self, reference: str, authority_ref: str) -> bool:
        _identifier(reference, "reference")
        _identifier(authority_ref, "authority_ref")
        with self.ledger._open() as connection:
            cursor = connection.execute("UPDATE agent_task_grants SET revoked_at=?,revocation_ref=? "
                "WHERE reference=? AND revoked_at IS NULL", (self.clock(), authority_ref, reference))
            return bool(cursor.rowcount)

    def _verify(self, connection, reference, capability, arguments, version, *,
                task_id, run_id, task_only=False):
        try:
            for value, name in ((reference, "reference"), (task_id, "task_id"), (run_id, "run_id")):
                _identifier(value, name)
            if type(arguments) is not dict:
                raise GrantError("arguments_must_be_object")
            _canonical(arguments)
            if not task_only:
                _version(version)
            if not task_only and (not isinstance(capability, str) or not _NAME.fullmatch(capability)):
                raise GrantError("invalid_capability")
        except ValueError:
            return GrantDecision(False, "invalid_grant_request")
        row = connection.execute("SELECT * FROM agent_task_grants WHERE reference=?", (reference,)).fetchone()
        if not row:
            return GrantDecision(False, "unknown_grant", reference)
        if row["task_id"] != task_id or row["run_id"] != run_id:
            return GrantDecision(False, "grant_task_run_mismatch", reference)
        if row["revoked_at"] is not None:
            return GrantDecision(False, "grant_revoked", reference)
        if row["expires_at"] is not None and self.clock() >= row["expires_at"]:
            return GrantDecision(False, "grant_expired", reference)
        task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if not task or not run or run["task_id"] != task_id:
            return GrantDecision(False, "task_run_mismatch", reference)
        if run["state"] in S.TERMINAL_STATES or run["finished_at"] is not None:
            return GrantDecision(False, "run_terminal", reference)
        if task["state"] != S.TASK_ACTIVE:
            return GrantDecision(False, "task_inactive", reference)
        try:
            fingerprint = _run_fingerprint(connection, task, run_id)
        except (ValueError, TypeError, KeyError):
            return GrantDecision(False, "task_binding_changed", reference)
        if fingerprint != row["task_fingerprint"]:
            return GrantDecision(False, "task_binding_changed", reference)
        try:
            capabilities = json.loads(row["capabilities"])
            binding = _grant_binding(task_id=task_id, run_id=run_id,
                fingerprint=row["task_fingerprint"], method=row["receipt_method"],
                receipt=row["receipt_reference"], authorizer=row["authorizer"],
                capabilities=capabilities, expires_at=row["expires_at"])
            if binding != row["binding_digest"]:
                raise GrantError("grant_binding_changed")
            if task_only:
                return GrantDecision(True, reference=reference, binding_digest=binding)
            entry = next((CapabilityGrant(**item) for item in capabilities if item["name"] == capability), None)
        except (ValueError, TypeError, KeyError):
            return GrantDecision(False, "grant_binding_invalid", reference)
        if entry is None or entry.version != version:
            return GrantDecision(False, "capability_not_granted", reference)
        if any(key not in arguments or _canonical(arguments[key]) != _canonical(value)
               for key, value in entry.constraints.items()):
            return GrantDecision(False, "resource_binding_changed", reference)
        binding = _digest("SOLVIO_TASK_EFFECT_V1", {
            "grant": reference, "grant_binding": row["binding_digest"], "capability": capability,
            "version": version, "arguments": arguments, "task_id": task_id, "run_id": run_id})
        return GrantDecision(True, reference=reference, binding_digest=binding)

    def verify(self, reference: str, capability: str, arguments: dict, version: int, *,
               task_id: str, run_id: str) -> GrantDecision:
        """Reine Lesung; sie beansprucht keinen Schritt und ersetzt keine Policy."""
        with self.ledger._open() as connection:
            connection.execute("BEGIN")
            return self._verify(connection, reference, capability, arguments, version,
                                task_id=task_id, run_id=run_id)

    def active(self, reference: str, *, task_id: str, run_id: str) -> GrantDecision:
        """Auftragsbindung vor Planung/Spezialisten; keine Capabilitybefugnis."""
        with self.ledger._open() as connection:
            connection.execute("BEGIN")
            return self._verify(connection, reference, None, {}, None,
                                task_id=task_id, run_id=run_id, task_only=True)

    def claim_step(self, reference: str, step_id: str, capability: str, arguments: dict,
                   version: int, *, task_id: str, run_id: str) -> GrantDecision:
        """Ein Anspruch, vor Wirkung, in DERSELBEN Transaktion wie die Pruefung."""
        _identifier(step_id, "step_id")
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            verdict = self._verify(connection, reference, capability, arguments, version,
                                   task_id=task_id, run_id=run_id)
            if not verdict.allowed:
                return verdict
            row = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (step_id,)).fetchone()
            if not row or row["run_id"] != run_id or row["kind"] != "capability" or row["capability"] != capability:
                return GrantDecision(False, "step_binding_mismatch", reference)
            binding = _digest("SOLVIO_TASK_STEP_DISPATCH_V1", {
                "effect": verdict.binding_digest, "step_id": step_id,
                "seq": row["seq"], "attempt": row["attempt"]})
            if row["dispatch_binding_digest"] or row["dispatch_claimed_at"] is not None:
                reason = ("step_already_claimed" if row["dispatch_binding_digest"] == binding
                          else "step_dispatch_binding_mismatch")
                return GrantDecision(False, reason, reference, binding)
            # Eine neue attempt-Zeile darf eine ungeklaerte alte Wirkung nicht
            # zur unberuehrten Aufgabe umetikettieren.
            previous = connection.execute("SELECT * FROM agent_steps WHERE run_id=? AND seq=? "
                "AND (dispatch_binding_digest<>'' OR dispatch_claimed_at IS NOT NULL)",
                (run_id, row["seq"])).fetchall()
            if previous:
                # Only a canonical native non-dispatch receipt can open a
                # fresh attempt of the same bound action. An error string,
                # owner resume or old receipt before a later UNKNOWN cannot.
                from solvio.agent_runtime import action_contract as AC
                if capability != AC.CAPABILITY or not all(AC.can_retry_step(
                        connection, self.ledger, old, task_id=task_id, run_id=run_id,
                        capability=capability, arguments=arguments, version=version)
                        for old in previous):
                    return GrantDecision(False, "step_attempt_already_claimed", reference, binding)
            run = connection.execute("SELECT state FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if run["state"] != S.RUNNING or row["state"] != "running" or row["finished_at"] is not None:
                return GrantDecision(False, "step_not_running", reference, binding)
            cursor = connection.execute("UPDATE agent_steps SET dispatch_binding_digest=?,dispatch_claimed_at=? "
                "WHERE step_id=? AND run_id=? AND capability=? AND state='running' "
                "AND dispatch_binding_digest='' AND dispatch_claimed_at IS NULL",
                (binding, self.clock(), step_id, run_id, capability))
            return GrantDecision(bool(cursor.rowcount), "" if cursor.rowcount else "step_already_claimed",
                                 reference, binding)
