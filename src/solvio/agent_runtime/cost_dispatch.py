"""Kosten-/Claim-Tor fuer jeden physischen CLI-Aufruf eines Auftrags.

Der Orchestrator setzt task_cost_scope um eine logische Planung, Bewertung oder
Spezialistenaktion. operation_id stammt aus seinem dauerhaften Ereignisstand;
alle physischen Aufrufe darin bekommen getrennte Ordnungsnummern, auch eine
Formatkorrektur. Derselbe Schluessel nach Neustart ist KEIN Wiederholungsrecht.

Ein frueherer UNKNOWN- oder verwaister CLAIMED-Aufruf sperrt weitere Dispatchs
des Auftrags, auch mit neuer operation_id. Ein vor Dispatch gehaltenes Budget-
oder Belegproblem kann dagegen nach Owner-Handlung einen neuen Ereignisversuch
bekommen. Dies ist ein Claim im bestehenden Agentenbuch, keine neue Laufzeit.

Ein Abo-Login ist kein Zusatzkostenbeleg. Quote-/Settlement-Adapter sind interne
Core-Dienste; Modell-/HTTP-Angaben werden hier niemals in solche Adapter verwandelt.
Ein Tokenzaehler oder --max-budget-usd ersetzt keine durchsetzbare EUR-Obergrenze.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import inspect
import itertools
import json
import re
import secrets
import time

from solvio.agent_runtime import costs as C, store as S, cost_subjects as A
from solvio.specialists.launcher import Invocation, LauncherError, Outcome

SCHEMA = A.INVOCATION_SCHEMA
_PROCESS_OWNER = secrets.token_hex(16)
_ACTIVE_CLAIMS: set[tuple[str, str]] = set()
_SCOPE: ContextVar["TaskCostScope | InteractionCostScope | None"] = ContextVar("solvio_task_cost_scope", default=None)


@dataclass(frozen=True)
class CostQuote:
    upper_bound_cents: int | None = None
    evidence: C.CostEvidence = C.CostEvidence("unknown")
    # Optional Core-owned last check, after source/auth awaits. The callback
    # is never serialized or accepted from HTTP/model data. Native account
    # observations expire and must still match the actual invocation here.
    validate_before_dispatch: object = None
    # Core-only account delegation from the already bound native credit quote.
    native_credit_account: str = ""

    def __post_init__(self):
        if self.upper_bound_cents is not None:
            C._cents(self.upper_bound_cents, "upper_bound")
        if type(self.evidence) is not C.CostEvidence:
            raise ValueError("cost_evidence_required")
        if self.validate_before_dispatch is not None and not callable(self.validate_before_dispatch):
            raise ValueError("invalid_quote_validator")
        if not isinstance(self.native_credit_account, str):
            raise ValueError("invalid_native_credit_delegation")
        if self.native_credit_account and (
                not re.fullmatch(r"[a-f0-9]{64}", self.native_credit_account)
                or self.evidence.kind != "owner_authorized_credits"
                or self.validate_before_dispatch is None):
            raise ValueError("invalid_native_credit_delegation")


@dataclass(frozen=True)
class CostSettlement:
    actual_cents: int | None
    evidence: C.CostEvidence


@dataclass
class DispatchResult:
    outcome: Outcome
    dispatch_started: bool
    reservation_id: str = ""
    invocation_id: str = ""
    cost_status: str = ""


@dataclass(frozen=True)
class ServiceInvocation:
    """Core-only service identity; payloads are represented by digests only.

    The calling service adapter must freeze the actual arguments/resources,
    compare them with these digests immediately before physical dispatch, and
    enforce the complete CapabilityGrant. This cost descriptor grants no
    capability authority and cannot attest a mutable callback's payload.
    """
    capability: str
    version: int
    service: str
    operation: str
    arguments_digest: str
    resources_digest: str

    def __post_init__(self):
        if (not isinstance(self.capability, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.capability)
                or type(self.version) is not int or not 1 <= self.version <= 1_000_000):
            raise ValueError("invalid_service_capability")
        for name in ("service", "operation"):
            C._text(getattr(self, name), name, 128)
        for name in ("arguments_digest", "resources_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("invalid_service_digest")

    @classmethod
    def bind(cls, *, capability, version, service, operation, arguments, resources):
        return cls(capability, version, service, operation,
                   _service_value_digest("arguments", arguments),
                   _service_value_digest("resources", resources))

    def matches(self, *, arguments, resources) -> bool:
        """For the trusted adapter's final payload check, after quote awaits."""
        return (self.arguments_digest == _service_value_digest("arguments", arguments)
                and self.resources_digest == _service_value_digest("resources", resources))

    @property
    def request_digest(self) -> str:
        return _service_value_digest("invocation", {
            "capability": self.capability, "version": self.version,
            "service": self.service, "operation": self.operation,
            "arguments_digest": self.arguments_digest,
            "resources_digest": self.resources_digest})


def _service_value_digest(kind, value):
    # Reuse the existing strict, bounded JSON and credential firewall. Nothing
    # from this temporary encoding is written to the cost ledger or a log.
    from solvio.agent_runtime.task_authority import _canonical
    if type(value) is not dict:
        raise ValueError("service_binding_requires_object")
    encoded = _canonical(value).encode("utf-8")
    return hashlib.sha256(b"SOLVIO_SERVICE_" + kind.encode("ascii") + b"_V1\0" + encoded).hexdigest()


@dataclass(frozen=True)
class ServiceOutcome:
    """An internal adapter's execution observation, not a process exit code.

    receipt_ref must name the adapter's actual terminal/non-dispatch evidence;
    a model or HTTP argument is never a receipt. Completed means the service
    invocation ended, not that the user's objective or remote effect is proven.
    Payload data is returned to the caller, never persisted by this cost layer.
    """
    state: str = "unknown"
    ok: bool = False
    reason: str = ""
    receipt_ref: str = ""
    data: object = None

    def __post_init__(self):
        if (self.state not in {"completed", "not_dispatched", "unknown"}
                or type(self.ok) is not bool or (self.ok and self.state != "completed")):
            raise ValueError("invalid_service_outcome")
        C._text(self.reason, "service_reason", 128, empty=True)
        C._text(self.receipt_ref, "service_receipt", 256, empty=self.state == "unknown")


@dataclass(frozen=True)
class ServiceDispatchResult:
    outcome: ServiceOutcome
    dispatch_started: bool
    reservation_id: str = ""
    invocation_id: str = ""
    cost_status: str = ""

    @property
    def completed(self) -> bool:
        return self.outcome.state == "completed"


class TaskCostScope:
    def __init__(self, ledger: S.AgentRunLedger, *, task_id: str, run_id: str,
                 phase: str, operation_id: str, quote_adapter=None, settlement_adapter=None):
        self.ledger = ledger
        self.task_id = C._text(task_id, "task_id")
        self.subject_id = self.task_id
        self.activity_id = None
        from solvio.agent_runtime.task_authority import TaskAuthority
        self.authority = TaskAuthority(ledger)
        grant = self.authority.for_run(run_id)
        self.grant_reference = grant.reference if grant else None
        self.run_id = C._text(run_id, "run_id")
        self.phase = C._text(phase, "phase")
        self.operation_id = C._text(operation_id, "operation_id")
        self.quote_adapter = quote_adapter
        self.settlement_adapter = settlement_adapter
        self.costs = C.CostLedger(ledger)
        self._ordinals = itertools.count(1)

    def next_invocation(self) -> tuple[int, str]:
        ordinal = next(self._ordinals)
        bound = [self.task_id, self.run_id, self.phase, self.operation_id, ordinal]
        digest = hashlib.sha256(json.dumps(bound, separators=(",", ":")).encode()).hexdigest()
        return ordinal, "pc-" + digest

    def continue_after_settled(self) -> int:
        """Fortsetzen HINTER abgeschlossenen physischen Versuchen DIESER Operation.

        Ein wiedereroeffneter Arbeiterschritt (N8/C4 §2.4/§2.6: bewiesener
        Nichtstart, `provider_wait` mit `resume_allowed`) traegt dieselbe
        `operation_id` (= step_id). Ein neuer Scope begaenne bei Ordinal 1 und
        traefe im Kostenbuch seinen gesettelten Vorgaenger — die Sperre, die
        fuer Neustarts gewollt ist, waere hier eine Sackgasse. Der Zaehler
        springt nur, wenn JEDER Vorgaenger terminal UND abgerechnet ist
        (`finished`+`settled` oder `not_dispatched`+`released`). Ein
        `claimed`, `unknown` oder `reserved` Vorgaenger laesst den Zaehler bei
        1: die Sperre bleibt. Kein Geld wird freigegeben, keine Zeile geaendert.
        Liefert die Zahl uebersprungener Ordinale.
        """
        with self.ledger._open() as connection:
            rows = connection.execute(
                "SELECT p.ordinal, p.state, c.state AS cost_state FROM agent_provider_invocations p "
                "LEFT JOIN agent_cost_reservations c ON c.reservation_id=p.reservation_id "
                "WHERE p.task_id=? AND p.run_id=? AND p.phase=? AND p.operation_id=?",
                (self.task_id, self.run_id, self.phase, self.operation_id)).fetchall()
        if not rows:
            return 0
        if any((row["state"], row["cost_state"]) not in {("finished", "settled"), ("not_dispatched", "released")}
               for row in rows):
            return 0
        highest = max(int(row["ordinal"]) for row in rows)
        self._ordinals = itertools.count(highest + 1)
        return highest


#: Zweck → Domaene der Invocation-Kennung. Ordinal 1 = Routing, Ordinal 2 =
#: Antwort (Textweg); jede physische Ausfuehrung ist ein eigener Claim derselben
#: Aktivitaet. Ein Neustart erzeugt einen neuen Scope mit Ordinal 1 — dieselbe
#: `invocation_id` trifft im Ledger auf ihren Vorgaenger, und genau das ist die
#: Sperre, keine Luecke.
INVOCATION_DOMAINS = {
    "adaptive_extract": "adaptive-extract-cost-invocation-v1",
    "voice_delegate": "voice-delegate-cost-invocation-v1",
    "text_chat": "text-chat-cost-invocation-v1",
}


class InteractionCostScope:
    """One persisted, explicitly purposed activity; no fabricated task or grant."""
    def __init__(self, ledger, *, activity_id, content_digest, quote_adapter=None, settlement_adapter=None):
        self.ledger = ledger
        self.costs = C.CostLedger(ledger)
        binding = A.ActivityLedger(ledger).binding(activity_id, content_digest=content_digest)
        self.generation = A.ActivityLedger(ledger).generation(activity_id)
        self.binding = binding
        self.subject_id = binding.subject_id
        self.task_id = binding.task_id
        self.run_id = binding.run_id
        self.activity_id = binding.activity_id
        self.content_digest = binding.content_digest
        self.phase = binding.purpose
        self.operation_id = binding.activity_id
        self.quote_adapter = quote_adapter
        self.settlement_adapter = settlement_adapter
        self._ordinals = itertools.count(1)

    def next_invocation(self):
        ordinal = next(self._ordinals)
        # Eine explizite Tabelle statt einer Zweierverzweigung: ein unbekannter
        # Zweck faellt hier durch, statt still die Sprachdomaene zu bekommen.
        try:
            domain = INVOCATION_DOMAINS[self.phase]
        except KeyError:
            raise ValueError("unsupported_activity_purpose") from None
        bound = [domain, self.subject_id, self.activity_id, self.generation, ordinal]
        digest = hashlib.sha256(json.dumps(bound, separators=(",", ":")).encode()).hexdigest()
        return ordinal, "pc-" + digest


@contextmanager
def interaction_cost_scope(ledger, *, activity_id, content_digest, quote_adapter=None, settlement_adapter=None):
    scope = InteractionCostScope(ledger, activity_id=activity_id, content_digest=content_digest,
        quote_adapter=quote_adapter, settlement_adapter=settlement_adapter)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


@contextmanager
def task_cost_scope(ledger: S.AgentRunLedger, *, task_id: str, run_id: str, phase: str,
                    operation_id: str, quote_adapter=None, settlement_adapter=None):
    scope = TaskCostScope(ledger, task_id=task_id, run_id=run_id, phase=phase,
        operation_id=operation_id, quote_adapter=quote_adapter, settlement_adapter=settlement_adapter)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> TaskCostScope | InteractionCostScope | None:
    return _SCOPE.get()


def active_task_invocation(connection, ledger, run_id):
    """Read the physical claim owned by this process, never a model's ID.

    The progress sink calls this inside its journal write transaction. This
    proves association only; a progress observation cannot settle this claim.
    """
    scope = current_scope()
    if (type(scope) is not TaskCostScope or scope.ledger is not ledger
            or scope.run_id != run_id or scope.phase != 'specialist'):
        return None
    rows = connection.execute("SELECT reservation_id,invocation_id,operation_id FROM "
        "agent_provider_invocations WHERE task_id=? AND run_id=? AND phase='specialist' "
        "AND operation_id=? AND provider IN ('codex','claude-code') AND state='claimed' AND process_owner=?",
        (scope.task_id, run_id, scope.operation_id, _PROCESS_OWNER)).fetchall()
    active = [r for r in rows if (ledger.path, r['reservation_id']) in _ACTIVE_CLAIMS]
    return dict(active[0]) if len(active) == 1 else None


def recovery_pending() -> bool:
    """Auch nach Abbruch eines Transports gilt der persistierte Dispatchbeleg.

    Die aufrufende Planung kann ihre aeussere Frist erreichen, ohne einen
    Antwortumschlag zu bekommen. Ein dort verlorenes Ergebnis macht einen
    bereits als ungewiss gebuchten Aufruf nicht wiederholbar.
    """
    scope = current_scope()
    if scope is None:
        return False
    with scope.ledger._open() as connection:
        return _recovery_pending(connection, scope)


def _recovery_pending(connection, scope) -> bool:
    for row in connection.execute("SELECT reservation_id,process_owner,state FROM "
                                  "agent_provider_invocations WHERE subject_id=? "
                                  "AND state IN ('claimed','unknown')", (scope.subject_id,)):
        if (row["state"] == "unknown" or row["process_owner"] != _PROCESS_OWNER or
                (scope.ledger.path, row["reservation_id"]) not in _ACTIVE_CLAIMS):
            return True
    return False


def _scope_reason(connection, scope) -> str:
    subject = connection.execute("SELECT revoked_at FROM agent_cost_subjects WHERE subject_id=?", (scope.subject_id,)).fetchone()
    if not subject or subject["revoked_at"] is not None:
        return "cost_recovery_required"
    if isinstance(scope, InteractionCostScope):
        reason = A.activity_reason(connection, scope.activity_id, scope.content_digest, generation=scope.generation)
        return reason or ("cost_recovery_required" if _recovery_pending(connection, scope) else "")
    row = connection.execute("SELECT r.state,r.finished_at,t.state AS task_state "
        "FROM agent_runs r JOIN agent_tasks t ON t.task_id=r.task_id "
        "WHERE r.run_id=? AND t.task_id=?", (scope.run_id, scope.task_id)).fetchone()
    if not row or row["task_state"] != S.TASK_ACTIVE:
        return "cost_recovery_required"
    if row["state"] in S.TERMINAL_STATES or row["finished_at"] is not None or row["state"] == S.WAITING_USER:
        return "cost_recovery_required"
    if scope.phase == "action_interpret":
        from solvio.agent_runtime.action_intent import cost_reason
        reason = cost_reason(connection, scope)
        return reason or ("cost_recovery_required" if _recovery_pending(connection, scope) else "")
    from solvio.agent_runtime import action_intent as AI
    try:
        intent = AI._read(connection, scope.run_id)
        if intent is not None and intent[2]["state"] != "resolved":
            return "cost_recovery_required"
    except (ValueError, TypeError, KeyError):
        return "cost_recovery_required"
    grant = connection.execute("SELECT reference FROM agent_task_grants WHERE run_id=?", (scope.run_id,)).fetchone()
    reference = scope.grant_reference or (grant["reference"] if grant else None)
    if reference and not scope.authority._verify(connection, reference, None, {}, None,
            task_id=scope.task_id, run_id=scope.run_id, task_only=True).allowed:
        return "cost_recovery_required"
    return "cost_recovery_required" if _recovery_pending(connection, scope) else ""


def _request_digest(provider, invocation, prompt) -> str:
    body = {"provider": provider, "invocation": asdict(invocation),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _claim(scope, decision, ordinal, invocation_id, provider, request_digest) -> bool:
    with scope.ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Ein reservierter Betrag allein darf keinen zweiten Dispatch tragen.
        if connection.execute("SELECT 1 FROM agent_provider_invocations WHERE reservation_id=?",
                              (decision.reservation_id,)).fetchone():
            return False
        reservation = connection.execute("SELECT state,subject_id,invocation_id,route FROM "
            "agent_cost_reservations WHERE reservation_id=?", (decision.reservation_id,)).fetchone()
        if (not reservation or reservation["state"] != "reserved" or
                (reservation["subject_id"], reservation["invocation_id"], reservation["route"]) !=
                (scope.subject_id, invocation_id, provider)):
            return False
        if _scope_reason(connection, scope):
            # Exact unclaimed reservation + the same write lock proves that no
            # invocation could have started. A concurrent cancellation/expiry
            # must not strand imaginary spend. Existing claims returned above.
            connection.execute("UPDATE agent_cost_reservations SET state='released',"
                "settlement_evidence=?,updated_at=? WHERE reservation_id=? AND state='reserved'",
                (C.CostEvidence("not_dispatched", "claim-refused:" + invocation_id).json(),
                 time.time(), decision.reservation_id))
            return False
        if any(value["overrun"] for value in scope.costs._totals(connection, scope.subject_id).values()):
            return False
        connection.execute("INSERT INTO agent_provider_invocations "
            "(reservation_id,invocation_id,subject_id,task_id,run_id,activity_id,phase,operation_id,ordinal,"
            "provider,request_digest,process_owner,state,claimed_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (decision.reservation_id, invocation_id, scope.subject_id, scope.task_id, scope.run_id, scope.activity_id,
             scope.phase, scope.operation_id, ordinal, provider, request_digest,
             _PROCESS_OWNER, "claimed", time.time()))
    _ACTIVE_CLAIMS.add((scope.ledger.path, decision.reservation_id))
    return True


def _finish(scope, reservation_id, state):
    with scope.ledger._open() as connection:
        connection.execute("UPDATE agent_provider_invocations SET state=?,finished_at=? "
            "WHERE reservation_id=? AND state='claimed'", (state, time.time(), reservation_id))


def _refusal(reason: str, *, invocation_id="", reservation_id="", status="") -> DispatchResult:
    return DispatchResult(Outcome(False, reason=reason, process_started=False), False,
                          reservation_id, invocation_id, status)


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _quote_still_valid(quote):
    if quote.validate_before_dispatch is None:
        return True
    try:
        checked = quote.validate_before_dispatch()
        if inspect.iscoroutine(checked):
            checked.close()
        return checked is True
    except Exception:
        return False


def _credit_worker_invocation(provider, invocation, quote):
    """Delegate before hashing/claiming the actual child argv, never a setting.

    Factories remain authority-free. Only the exact, already quoted native
    worker receives the proof; existing Codex CLI calls need no flag.
    """
    from pathlib import Path
    flag = "--authorized-credit-account"
    if any(arg == flag or arg.startswith(flag + "=") for arg in invocation.argv):
        raise ValueError("unbound_native_credit_delegation")
    if not quote.native_credit_account:
        return invocation
    if provider != "codex":
        raise ValueError("invalid_native_credit_provider")
    if invocation.argv[:1] == ("exec",):
        return invocation
    from solvio.agent_runtime.native_costs import _known_invocation
    if (invocation.argv[:2] != ("-I", "-B") or len(invocation.argv) < 3
            or Path(invocation.argv[2]).name not in {
                "hermes_native_worker.py", "hermes_research_worker.py", "image_generation_worker.py"}
            or not _known_invocation(provider, invocation)):
        raise ValueError("invalid_native_credit_worker")
    return replace(invocation, argv=invocation.argv + (flag, quote.native_credit_account))


async def dispatch(provider: str, invocation: Invocation, prompt: str, runner) -> DispatchResult:
    """Nach erfolgreicher Authpruefung, direkt vor dem vorhandenen Starter."""
    if any(arg == "--authorized-credit-account" or arg.startswith("--authorized-credit-account=")
           for arg in invocation.argv):
        return _refusal("cost_unbounded")
    scope = current_scope()
    if scope is None:
        # Andere Produktwege sind noch nicht an auftragsweite Kosten gebunden.
        # Der produktive Orchestrator MUSS den Scope setzen; kein erfundener Task.
        result = await runner(invocation, prompt)
        return DispatchResult(result, result.process_started is not False)
    ordinal, invocation_id = scope.next_invocation()
    with scope.ledger._open() as connection:
        if _scope_reason(connection, scope):
            return _refusal("cost_recovery_required", invocation_id=invocation_id)
    try:
        quote = (await _maybe_await(scope.quote_adapter(provider, invocation))
                 if scope.quote_adapter else CostQuote())
        if type(quote) is not CostQuote:
            raise ValueError("invalid_cost_quote")
    except LauncherError as exc:
        return _refusal("quota" if exc.reason == "quota" else "cost_unbounded",
                        invocation_id=invocation_id)
    except Exception:
        return _refusal("cost_unbounded", invocation_id=invocation_id)
    # Interactive sources live in the existing device/session authority store.
    # Revalidate after auth/quote awaits, immediately before money and the
    # physical claim. This is a Core-only callback, never a provider argument.
    source_check = getattr(scope, "source_check", None)
    if source_check is not None:
        try:
            await _maybe_await(source_check())
        except Exception:
            return _refusal("cost_recovery_required", invocation_id=invocation_id)
    # Deliberately synchronous: no further scheduling gap before the existing
    # reservation and physical claim transactions.
    if not _quote_still_valid(quote):
        return _refusal("cost_unbounded", invocation_id=invocation_id)
    try:
        invocation = _credit_worker_invocation(provider, invocation, quote)
    except ValueError:
        return _refusal("cost_unbounded", invocation_id=invocation_id)
    decision = scope.costs.reserve_subject(scope.subject_id, invocation_id, quote.upper_bound_cents,
                                   route=provider, evidence=quote.evidence)
    if not decision.allowed:
        reason = {"unbounded_cost": "cost_unbounded", "approval_required": "cost_approval_required"}.get(
            decision.status, "cost_recovery_required")
        return _refusal(reason, invocation_id=invocation_id,
                        reservation_id=decision.reservation_id, status=decision.status)
    digest = _request_digest(provider, invocation, prompt)
    if not _claim(scope, decision, ordinal, invocation_id, provider, digest):
        with scope.ledger._open() as connection:
            reservation = connection.execute("SELECT state FROM agent_cost_reservations WHERE reservation_id=?",
                                             (decision.reservation_id,)).fetchone()
        return _refusal("cost_recovery_required", invocation_id=invocation_id,
                        reservation_id=decision.reservation_id,
                        status=reservation["state"] if reservation else "")
    reservation_id = decision.reservation_id
    try:
        result = await runner(invocation, prompt)
        not_dispatched = (result.process_started is False and not result.ok and result.exit_code is None)
        if not_dispatched:
            scope.costs.release(reservation_id, C.CostEvidence("not_dispatched", "launcher:" + invocation_id))
            _finish(scope, reservation_id, "not_dispatched")
            return DispatchResult(result, False, reservation_id, invocation_id, "released")
        if result.exit_code is None:
            scope.costs.mark_unknown(reservation_id)
            _finish(scope, reservation_id, "unknown")
            return DispatchResult(result, True, reservation_id, invocation_id, "unknown")
        settlement = None
        if scope.settlement_adapter:
            try:
                settlement = await _maybe_await(scope.settlement_adapter(provider, invocation, result))
            except Exception:
                settlement = None
        if settlement is None and quote.upper_bound_cents == 0 and quote.evidence.kind in C.ZERO_COST_EVIDENCE:
            settlement = CostSettlement(0, quote.evidence)
        elif settlement is None and quote.evidence.kind == "owner_authorized_credits":
            # Terminal process, but no per-turn credit receipt. NULL is honest;
            # never add credits to ZERO_COST_EVIDENCE or invent a EUR charge.
            settlement = CostSettlement(None, quote.evidence)
        status = "reserved"
        if type(settlement) is CostSettlement:
            try:
                settled = scope.costs.settle(reservation_id, settlement.actual_cents, settlement.evidence)
                status = settled.status
            except ValueError:
                # Eine unbrauchbare Abrechnung gibt kein Geld frei.
                status = "reserved"
        _finish(scope, reservation_id, "finished")
        return DispatchResult(result, True, reservation_id, invocation_id, status)
    except BaseException:
        # Auch Cancellation bleibt nach Claim ungewiss. Der bestehende Starter
        # beendet/reapt erst seine Prozessgruppe; hier wird nur die Wahrheit
        # ueber die verbleibende Kostenreserve festgehalten.
        scope.costs.mark_unknown(reservation_id)
        _finish(scope, reservation_id, "unknown")
        raise
    finally:
        _ACTIVE_CLAIMS.discard((scope.ledger.path, reservation_id))


def _service_refusal(reason, *, invocation_id="", reservation_id="", status=""):
    return ServiceDispatchResult(ServiceOutcome("not_dispatched", reason=reason,
        receipt_ref="core:service-cost-gate"), False, reservation_id, invocation_id, status)


async def dispatch_service(invocation: ServiceInvocation, runner, *, quote_adapter=None) -> ServiceDispatchResult:
    """Bound service dispatch in the existing task/claim/cost ledger only.

    Unlike the legacy CLI entry point, there is no unscoped mode, interaction
    scope or grant-free task mode. Quote/settlement adapters are Core services
    with signatures (service, invocation[, outcome]); they must not themselves
    perform the service operation. An explicit Core registration quote can
    replace the scope's general provider quote, never its task/cost authority.
    The callback receives the immutable digest
    descriptor and owns payload, capability authority and service cancellation.
    """
    if type(invocation) is not ServiceInvocation:
        raise ValueError("typed_service_invocation_required")
    invocation.__post_init__()
    scope = current_scope()
    if (type(scope) is not TaskCostScope or scope.phase != "capability"
            or not scope.grant_reference):
        return _service_refusal("cost_unbounded")
    ordinal, invocation_id = scope.next_invocation()
    with scope.ledger._open() as connection:
        if _scope_reason(connection, scope):
            return _service_refusal("cost_recovery_required", invocation_id=invocation_id)
    try:
        pricing = quote_adapter if quote_adapter is not None else scope.quote_adapter
        quote = (await _maybe_await(pricing(invocation.service, invocation))
                 if pricing else CostQuote())
        if type(quote) is not CostQuote:
            raise ValueError("invalid_cost_quote")
    except Exception:
        return _service_refusal("cost_unbounded", invocation_id=invocation_id)
    source_check = getattr(scope, "source_check", None)
    if source_check is not None:
        try:
            await _maybe_await(source_check())
        except Exception:
            return _service_refusal("cost_recovery_required", invocation_id=invocation_id)
    if not _quote_still_valid(quote):
        return _service_refusal("cost_unbounded", invocation_id=invocation_id)
    decision = scope.costs.reserve_subject(scope.subject_id, invocation_id,
        quote.upper_bound_cents, route=invocation.service, evidence=quote.evidence)
    if not decision.allowed:
        reason = {"unbounded_cost": "cost_unbounded", "approval_required": "cost_approval_required"}.get(
            decision.status, "cost_recovery_required")
        return _service_refusal(reason, invocation_id=invocation_id,
            reservation_id=decision.reservation_id, status=decision.status)
    if not _claim(scope, decision, ordinal, invocation_id, invocation.service, invocation.request_digest):
        with scope.ledger._open() as connection:
            row = connection.execute("SELECT state FROM agent_cost_reservations WHERE reservation_id=?",
                                     (decision.reservation_id,)).fetchone()
        return _service_refusal("cost_recovery_required", invocation_id=invocation_id,
            reservation_id=decision.reservation_id, status=row["state"] if row else "")
    reservation_id = decision.reservation_id
    try:
        result = await runner(invocation)
        if type(result) is not ServiceOutcome:
            raise ValueError("typed_service_outcome_required")
        result.__post_init__()
        if result.state == "not_dispatched":
            scope.costs.release(reservation_id, C.CostEvidence("not_dispatched", result.receipt_ref))
            _finish(scope, reservation_id, "not_dispatched")
            return ServiceDispatchResult(result, False, reservation_id, invocation_id, "released")
        if result.state == "unknown":
            scope.costs.mark_unknown(reservation_id)
            _finish(scope, reservation_id, "unknown")
            return ServiceDispatchResult(result, True, reservation_id, invocation_id, "unknown")
        settlement = None
        if scope.settlement_adapter:
            try:
                settlement = await _maybe_await(scope.settlement_adapter(invocation.service, invocation, result))
            except Exception:
                pass
        if settlement is None and quote.upper_bound_cents == 0 and quote.evidence.kind in C.ZERO_COST_EVIDENCE:
            settlement = CostSettlement(0, quote.evidence)
        status = "reserved"
        if type(settlement) is CostSettlement:
            try:
                status = scope.costs.settle(reservation_id, settlement.actual_cents, settlement.evidence).status
            except ValueError:
                pass  # Invalid accounting never releases a reservation.
        _finish(scope, reservation_id, "finished")
        return ServiceDispatchResult(result, True, reservation_id, invocation_id, status)
    except BaseException:
        scope.costs.mark_unknown(reservation_id)
        _finish(scope, reservation_id, "unknown")
        raise
    finally:
        _ACTIVE_CLAIMS.discard((scope.ledger.path, reservation_id))


def invocations(ledger: S.AgentRunLedger, task_id: str) -> list[dict]:
    """Belege ohne Prompt, Antwort oder Zugangsmaterial."""
    C._text(task_id, "task_id")
    with ledger._open() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_provider_invocations "
            "WHERE task_id=? ORDER BY claimed_at,reservation_id", (task_id,))]


def subject_invocations(ledger, subject_id):
    """Typed-subject projection, also valid for standalone interactions."""
    C._text(subject_id, "subject_id")
    with ledger._open() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_provider_invocations "
            "WHERE subject_id=? ORDER BY claimed_at,reservation_id", (subject_id,))]
