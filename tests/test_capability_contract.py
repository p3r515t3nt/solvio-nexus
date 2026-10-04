"""Capability Contract + TrustContext V1 — Verhaltenstests.

Die Frage, die diese Suite beantwortet, ist nicht „laeuft der Code durch", sondern:
**kann fremder Inhalt SOLVIO zum Handeln bringen?** Deshalb steht der Kern in den
Negativ-Tests: derselbe Satz, einmal vom Nutzer gesprochen und einmal aus einer
E-Mail gelesen, muss zu zwei verschiedenen Ergebnissen fuehren.
"""
import asyncio
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402
enforce_assertions()

from solvio.capabilities import (  # noqa: E402
    ArgumentSource, CapabilityOutcome, CapabilityResult, CapabilityRouter,
    CapabilitySpec, ExecutionClass, ExecutorUnavailable, AmbiguousExecution,
    authority_refusal, binding_digest, effective_risk, requires_approval,
)
from solvio.capabilities.compat import handler_for_tool, spec_for_tool  # noqa: E402
from solvio.contracts.trust import TrustContext, TrustLevel  # noqa: E402
from solvio.security.approval import ApprovalBroker  # noqa: E402
from solvio.security.mobile_approval.execution import (  # noqa: E402
    CAPABILITY_SEMANTICS, IDEMPOTENT_WRITE, NON_IDEMPOTENT_WRITE, READ_ONLY,
    RECONCILABLE_WRITE,
)
from solvio.tools.base import RiskLevel, ToolResult  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# -- Bausteine ---------------------------------------------------------------

def _read_spec(name="probe_read", **kw):
    base = dict(name=name, version=1, execution_class=ExecutionClass.FAST,
                base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
                input_schema={"type": "object",
                              "properties": {"entity": {"type": "string"}},
                              "required": ["entity"]})
    base.update(kw)
    return CapabilitySpec(**base)


def _write_spec(name="probe_write", **kw):
    base = dict(name=name, version=1, execution_class=ExecutionClass.CONTROLLED,
                base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
                input_schema={"type": "object",
                              "properties": {"entity": {"type": "string"}},
                              "required": ["entity"]})
    base.update(kw)
    return CapabilitySpec(**base)


def _user_turn():
    """Ein echter Nutzer-Akt: er hat es gerade selbst gesagt."""
    return TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True)


def _email_turn():
    """Der Inhalt kam aus einer E-Mail. Information, keine Autoritaet."""
    return TrustContext(origin_trust=TrustLevel.UNTRUSTED_EMAIL, user_authorized=True)


def _web_turn():
    return TrustContext(origin_trust=TrustLevel.UNTRUSTED_WEB, user_authorized=True)


def _model_turn():
    """Das Modell hat es selbst hergeleitet."""
    return TrustContext(origin_trust=TrustLevel.AGENT_GENERATED, user_authorized=True)


class _Approver:
    """Ein vertrauenswuerdiger Kanal — steht fuer den iPhone-Weg."""

    def __init__(self, identity="owner"):
        self.identity = identity

    def is_trusted(self, request, identity):
        return identity == self.identity


def _approved_broker():
    """Broker mit vertrauenswuerdigem Approver."""
    return ApprovalBroker(approver=_Approver())


def _grant(broker, router, spec, args, identity="owner"):
    """Holt eine Freigabe auf dem Control-Plane-Weg und gibt die request_id zurueck."""
    pending = [p for p in broker.list_pending() if p["tool"] == spec.name]
    require(pending, "keine offene Freigabe zum Bestaetigen")
    entry = pending[-1]
    approved, status = broker.approve(request_id=entry["request_id"],
                                      identity=identity,
                                      presented_digest=entry["digest"])
    require_equal(status, "ok", "Freigabe scheiterte")
    return approved.request_id


# =====================================================================
# Trust — der Kern
# =====================================================================

def t_direct_user_may_authorize_a_write():
    """Der Nutzer sagt es selbst: die Aktion darf (nach Freigabe) laufen."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    seen = []
    router.register(spec, lambda a: seen.append(a) or {"done": True})
    args = {"entity": "licht"}

    first = _run(router.execute(spec.name, args, trust=_user_turn(),
                                provenance={"entity": ArgumentSource.USER_DIRECT}))
    require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(first))
    require(not seen, "die Aktion lief VOR der Freigabe")

    request_id = _grant(broker, router, spec, args)
    second = _run(router.execute(spec.name, args, trust=_user_turn(),
                                 provenance={"entity": ArgumentSource.USER_DIRECT},
                                 approval_request_id=request_id))
    require_equal(second.outcome, CapabilityOutcome.SUCCESS, str(second))
    require_equal(len(seen), 1, "die Aktion lief nicht genau einmal")


def t_the_same_command_from_email_creates_no_authority():
    """DER kritische Test: identischer Befehl, andere Herkunft, anderes Ergebnis."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"entity": "haustuer"}

    result = _run(router.execute(spec.name, args, trust=_email_turn(),
                                 provenance={"entity": ArgumentSource.UNTRUSTED_CONTENT}))
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "untrusted_origin", str(result))
    require(not ran, "eine E-Mail hat eine Aktion ausgeloest")
    # Und sie darf nicht einmal eine Bestaetigungsfrage erzeugen: kein Zermuerben.
    require_equal(broker.pending_count(), 0,
                  "fremder Inhalt hat eine Freigabeanfrage erzeugt")


def t_web_content_cannot_authorize_even_with_user_authorized_flag():
    """`user_authorized=True` allein reicht nicht — die Herkunft muss tragen."""
    spec = _write_spec()
    # Genau die Konstruktion, die ein naiver Aufrufer machen wuerde.
    trust = TrustContext(origin_trust=TrustLevel.UNTRUSTED_WEB, user_authorized=True)
    require(not trust.may_authorize(), "Webinhalt trug Autoritaet")
    require_equal(authority_refusal(spec, trust, RiskLevel.CRITICAL), "untrusted_origin")


def t_model_generated_content_cannot_upgrade_its_own_authority():
    """Was das Modell selbst erzeugt hat, autorisiert nichts."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_model_turn(),
                                 provenance={"entity": ArgumentSource.MODEL_DERIVED}))
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "no_user_authority", str(result))
    require(not ran, "modellerzeugter Kontext hat ausgefuehrt")


def t_arguments_are_never_authority():
    """Ein Argument namens `confirmed` aendert nichts — Autoritaet kommt nie von dort.

    Der alte Dispatcher-Pfad liest `arguments['confirmed']`, ein Feld, das das Modell
    selbst fuellt. Dieser Vertrag darf darauf nicht hereinfallen.
    """
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec(input_schema={"type": "object",
                                     "properties": {"entity": {"type": "string"},
                                                    "confirmed": {"type": "boolean"},
                                                    "user_authorized": {"type": "boolean"}},
                                     "required": ["entity"]})
    ran = []
    router.register(spec, lambda a: ran.append(a))
    result = _run(router.execute(
        spec.name, {"entity": "x", "confirmed": True, "user_authorized": True},
        trust=_email_turn(), provenance={"entity": ArgumentSource.UNTRUSTED_CONTENT}))
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "untrusted_origin", str(result))
    require(not ran, "ein Argument hat als Autoritaet gewirkt")


def t_reading_stays_possible_from_untrusted_origin():
    """Information ja, Autoritaet nein: Lesen bleibt erlaubt.

    Sonst waere die Regel nutzlos — SOLVIO soll ueber eine E-Mail sprechen koennen,
    nur nicht auf sie hin handeln.
    """
    router = CapabilityRouter(approvals=_approved_broker())
    spec = _read_spec()
    router.register(spec, lambda a: {"state": "zu"})
    result = _run(router.execute(spec.name, {"entity": "haustuer"}, trust=_email_turn(),
                                 provenance={"entity": ArgumentSource.UNTRUSTED_CONTENT}))
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))


def t_trusted_internal_state_does_not_authorize_writes():
    """LOCAL_TRUSTED_TOOL (z. B. ein HA-Messwert) ist vertrauenswuerdig, aber nicht Autoritaet."""
    spec = _write_spec()
    trust = TrustContext(origin_trust=TrustLevel.LOCAL_TRUSTED_TOOL, user_authorized=True)
    require_equal(authority_refusal(spec, trust, RiskLevel.MUTATING), "no_user_authority")
    # Lesen darf es sehr wohl.
    require_equal(authority_refusal(_read_spec(), trust, RiskLevel.HARMLESS), None)


def t_system_trusted_origin_still_needs_a_user_act():
    """SYSTEM_TRUSTED traegt Autoritaet — aber nur mit einem echten Nutzerakt."""
    spec = _write_spec()
    without = TrustContext(origin_trust=TrustLevel.SYSTEM_TRUSTED, user_authorized=False)
    with_act = TrustContext(origin_trust=TrustLevel.SYSTEM_TRUSTED, user_authorized=True)
    require_equal(authority_refusal(spec, without, RiskLevel.MUTATING), "no_user_authority")
    require_equal(authority_refusal(spec, with_act, RiskLevel.MUTATING), None)


# =====================================================================
# Effektives Risiko
# =====================================================================

def t_base_risk_is_preserved_without_provenance():
    require_equal(effective_risk(_write_spec(), None), RiskLevel.MUTATING)
    require_equal(effective_risk(_write_spec(), {}), RiskLevel.MUTATING)
    require_equal(effective_risk(_read_spec(), None), RiskLevel.HARMLESS)


def t_model_derived_argument_raises_effective_risk():
    spec = _write_spec()
    user = effective_risk(spec, {"entity": ArgumentSource.USER_DIRECT})
    model = effective_risk(spec, {"entity": ArgumentSource.MODEL_DERIVED})
    require_equal(user, RiskLevel.MUTATING)
    require_equal(model, RiskLevel.CRITICAL, "modellgewaehltes Argument hob das Risiko nicht")
    require(int(model) > int(user), "Herkunft hatte keine Wirkung")


def t_untrusted_argument_raises_to_critical():
    spec = _write_spec(base_risk=RiskLevel.MUTATING)
    require_equal(effective_risk(spec, {"entity": ArgumentSource.UNTRUSTED_CONTENT}),
                  RiskLevel.CRITICAL)


def t_effective_risk_never_falls_below_base():
    """Fremder Inhalt darf das Gate nicht entschaerfen — nur verschaerfen."""
    for base in (RiskLevel.HARMLESS, RiskLevel.MUTATING, RiskLevel.CRITICAL):
        spec = _write_spec(base_risk=base)
        for source in ArgumentSource:
            got = effective_risk(spec, {"entity": source})
            require(int(got) >= int(base),
                    f"{source.value} senkte das Risiko von {base.name} auf {got.name}")


def t_worst_argument_wins_across_several():
    spec = _write_spec(input_schema={"type": "object",
                                     "properties": {"a": {"type": "string"},
                                                    "b": {"type": "string"}}})
    mixed = {"a": ArgumentSource.USER_DIRECT, "b": ArgumentSource.UNTRUSTED_CONTENT}
    require_equal(effective_risk(spec, mixed), RiskLevel.CRITICAL,
                  "ein einziges unvertrauenswuerdiges Argument reichte nicht")


def t_reading_does_not_escalate():
    """Ein Lesevorgang hat keine Aussenwirkung, also gibt es nichts zu bestaetigen."""
    require_equal(effective_risk(_read_spec(), {"entity": ArgumentSource.UNTRUSTED_CONTENT}),
                  RiskLevel.HARMLESS)


def t_approval_threshold_is_mutating_and_above():
    require(not requires_approval(RiskLevel.HARMLESS))
    require(requires_approval(RiskLevel.MUTATING))
    require(requires_approval(RiskLevel.CRITICAL))


# =====================================================================
# Freigabe
# =====================================================================

def t_router_delegates_to_the_existing_broker():
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    router.register(spec, lambda a: {"ok": True})
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))
    require_equal(broker.pending_count(), 1, "der Broker sah die Anforderung nicht")
    require(result.data and result.data.get("request_id"), "keine request_id geliefert")


def t_without_a_broker_nothing_effective_runs():
    """Fail-closed: ohne Freigabekanal keine Wirkung."""
    router = CapabilityRouter(approvals=None)
    spec = _write_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "no_approval_channel", str(result))
    require(not ran, "ohne Freigabekanal wurde ausgefuehrt")


def t_a_request_id_alone_executes_nothing():
    """Die request_id ist ein Identifier, keine Autoritaet."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"entity": "x"}
    first = _run(router.execute(spec.name, args, trust=_user_turn()))
    request_id = first.data["request_id"]
    # Kein approve() dazwischen — die id ist bekannt, aber nichts ist freigegeben.
    second = _run(router.execute(spec.name, args, trust=_user_turn(),
                                 approval_request_id=request_id))
    require_equal(second.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(second))
    require_equal(second.reason, "approval_not_approved", str(second))
    require(not ran, "eine unbestaetigte request_id hat ausgefuehrt")


def t_changed_arguments_invalidate_an_existing_approval():
    """Drift: freigegeben wurde Aktion A, ausgefuehrt werden soll B."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    approved_args = {"entity": "leselampe"}
    _run(router.execute(spec.name, approved_args, trust=_user_turn()))
    request_id = _grant(broker, router, spec, approved_args)

    drifted = _run(router.execute(spec.name, {"entity": "haustuer"}, trust=_user_turn(),
                                  approval_request_id=request_id))
    require_equal(drifted.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(drifted))
    require_equal(drifted.reason, "approval_drift", str(drifted))
    require(not ran, "eine fremde Aktion lief unter einer erteilten Freigabe")


def t_an_approval_is_single_use():
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec()
    calls = []
    router.register(spec, lambda a: calls.append(a))
    args = {"entity": "licht"}
    _run(router.execute(spec.name, args, trust=_user_turn()))
    request_id = _grant(broker, router, spec, args)
    first = _run(router.execute(spec.name, args, trust=_user_turn(),
                                approval_request_id=request_id))
    second = _run(router.execute(spec.name, args, trust=_user_turn(),
                                 approval_request_id=request_id))
    require_equal(first.outcome, CapabilityOutcome.SUCCESS, str(first))
    require_equal(second.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(second))
    require_equal(len(calls), 1, "die Freigabe wirkte zweimal")


def t_the_binding_digest_changes_with_every_relevant_field():
    spec = _write_spec()
    base = binding_digest(spec, {"entity": "a"})
    require(base != binding_digest(spec, {"entity": "b"}), "Argument aenderte den Digest nicht")
    require(base != binding_digest(_write_spec(version=2), {"entity": "a"}),
            "Version aenderte den Digest nicht")
    require(base != binding_digest(_write_spec(name="anderer"), {"entity": "a"}),
            "Name aenderte den Digest nicht")
    # Stabil gegen Schluesselreihenfolge — sonst waere jede Freigabe zufaellig ungueltig.
    two_a = binding_digest(spec, {"entity": "a", "helligkeit": 5})
    two_b = binding_digest(spec, {"helligkeit": 5, "entity": "a"})
    require_equal(two_a, two_b, "die Schluesselreihenfolge veraenderte den Digest")


def t_the_model_cannot_approve():
    """Es gibt keinen Weg vom Router zu `approve()`."""
    import inspect
    from solvio.capabilities import router as router_module
    source = inspect.getsource(router_module)
    require(".approve(" not in source,
            "der Router ruft approve() — Freigabe gehoert ausschliesslich dem Approver")
    # Und ohne vertrauenswuerdigen Approver gibt der Broker nichts frei.
    bare = ApprovalBroker()
    r = bare.request(principal="voice", tool="probe_write", task="t", workspace="",
                     mode="controlled")
    approved, status = bare.approve(request_id=r.request_id, identity="wer-auch-immer",
                                    presented_digest=r.digest)
    require_equal(status, "no_trusted_approver", "ohne Approver wurde freigegeben")
    require(approved is None)


# =====================================================================
# Idempotenz / Nebenwirkungen
# =====================================================================

def t_the_frozen_pinned_semantics_cannot_be_weakened():
    """Eine Spec darf eine eingefrorene Klassifizierung nicht aufweichen."""
    for name in CAPABILITY_SEMANTICS:
        spec = CapabilitySpec(name=name, version=1,
                              execution_class=ExecutionClass.DEEP,
                              base_risk=RiskLevel.CRITICAL, semantics=IDEMPOTENT_WRITE)
        require_equal(spec.effective_semantics(), CAPABILITY_SEMANTICS[name],
                      f"{name} wurde durch eine Spec aufgeweicht")


def t_unpinned_capabilities_keep_their_declared_semantics():
    """Sonst waere jede neue Lese-Faehigkeit sofort ein nicht-idempotenter Schreibvorgang."""
    require_equal(_read_spec(name="frisch_gelesen").effective_semantics(), READ_ONLY)
    require_equal(_write_spec(name="frisch_geschrieben", semantics=IDEMPOTENT_WRITE)
                  .effective_semantics(), IDEMPOTENT_WRITE)


def t_all_three_effect_classes_are_expressible():
    require(_read_spec(name="lesen").is_read_only())
    idem = _write_spec(name="idem", semantics=IDEMPOTENT_WRITE)
    nonidem = _write_spec(name="nonidem", semantics=NON_IDEMPOTENT_WRITE)
    recon = _write_spec(name="recon", semantics=RECONCILABLE_WRITE)
    require(not idem.is_read_only() and not nonidem.is_read_only())
    require_equal(idem.effective_semantics(), IDEMPOTENT_WRITE)
    require_equal(nonidem.effective_semantics(), NON_IDEMPOTENT_WRITE)
    require_equal(recon.effective_semantics(), RECONCILABLE_WRITE)


def t_a_fast_capability_may_not_write():
    """FAST heisst „ohne Rueckfrage" — das darf keine Umgehung des Bestaetigungswegs sein."""
    err = require_raises(ValueError, CapabilitySpec, name="schnell_schreiben", version=1,
                         execution_class=ExecutionClass.FAST,
                         base_risk=RiskLevel.HARMLESS, semantics=NON_IDEMPOTENT_WRITE)
    require("READ_ONLY" in str(err), str(err))


def t_an_ambiguous_non_idempotent_outcome_demands_recovery():
    """Nicht „Erfolg", nicht „Fehlschlag" — sondern: ich weiss es nicht."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec(semantics=NON_IDEMPOTENT_WRITE)

    def _ambiguous(_args):
        raise AmbiguousExecution("Verbindung waehrend des Sendens verloren")

    router.register(spec, _ambiguous)
    args = {"entity": "x"}
    _run(router.execute(spec.name, args, trust=_user_turn()))
    request_id = _grant(broker, router, spec, args)
    result = _run(router.execute(spec.name, args, trust=_user_turn(),
                                 approval_request_id=request_id))
    require_equal(result.outcome, CapabilityOutcome.RECOVERY_REQUIRED, str(result))
    require(not result.had_no_effect, "ein mehrdeutiger Ausgang wurde als wirkungslos gemeldet")


def t_an_ambiguous_idempotent_outcome_stays_retryable():
    """Bei zugesicherter Deduplizierung ist ein Timeout kein Fall fuer den Menschen."""
    broker = _approved_broker()
    router = CapabilityRouter(approvals=broker)
    spec = _write_spec(name="idem_write", semantics=IDEMPOTENT_WRITE)

    def _ambiguous(_args):
        raise AmbiguousExecution("keine Antwort")

    router.register(spec, _ambiguous)
    args = {"entity": "x"}
    _run(router.execute(spec.name, args, trust=_user_turn()))
    request_id = _grant(broker, router, spec, args)
    result = _run(router.execute(spec.name, args, trust=_user_turn(),
                                 approval_request_id=request_id))
    require_equal(result.outcome, CapabilityOutcome.TIMEOUT, str(result))
    require(result.outcome is not CapabilityOutcome.RECOVERY_REQUIRED,
            "idempotenter Schreibvorgang verlangte manuelle Erholung")


# =====================================================================
# Ergebnis-Umschlag
# =====================================================================

def t_success_carries_data_and_an_identity():
    router = CapabilityRouter()
    spec = _read_spec()
    router.register(spec, lambda a: {"state": "an"})
    result = _run(router.execute(spec.name, {"entity": "licht"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.SUCCESS)
    require_equal(result.data, {"state": "an"})
    require(result.call_id.startswith("c-"), result.call_id)
    require_equal(result.as_dict()["success"], True)


def t_invalid_input_is_its_own_outcome():
    router = CapabilityRouter()
    spec = _read_spec()
    router.register(spec, lambda a: {"nie": True})
    missing = _run(router.execute(spec.name, {}, trust=_user_turn()))
    require_equal(missing.outcome, CapabilityOutcome.INVALID_INPUT, str(missing))
    require_equal(missing.reason, "missing_argument:entity")
    extra = _run(router.execute(spec.name, {"entity": "a", "schmuggel": 1},
                                trust=_user_turn()))
    require_equal(extra.outcome, CapabilityOutcome.INVALID_INPUT, str(extra))
    require_equal(extra.reason, "unknown_argument:schmuggel")
    wrong = _run(router.execute(spec.name, {"entity": 5}, trust=_user_turn()))
    require_equal(wrong.reason, "wrong_type:entity", str(wrong))


def t_an_unknown_capability_is_refused():
    router = CapabilityRouter()
    result = _run(router.execute("gibt_es_nicht", {}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY)
    require_equal(result.reason, "unknown_capability")


def t_timeout_is_distinct_and_honest():
    router = CapabilityRouter()
    spec = _read_spec(timeout=0.05)

    async def _slow(_args):
        await asyncio.sleep(5)

    router.register(spec, _slow)
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.TIMEOUT, str(result))
    require_equal(result.reason, "timeout")


def t_cancellation_yields_cancelled_not_failure():
    router = CapabilityRouter()
    spec = _read_spec(cancellable=True, timeout=5.0)

    async def _slow(_args):
        await asyncio.sleep(5)

    router.register(spec, _slow)

    async def _drive():
        token = asyncio.Event()

        async def _cancel_soon():
            await asyncio.sleep(0.02)
            token.set()

        task = asyncio.ensure_future(
            router.execute(spec.name, {"entity": "x"}, trust=_user_turn(),
                           cancel_token=token))
        await _cancel_soon()
        return await task

    result = _run(_drive())
    require_equal(result.outcome, CapabilityOutcome.CANCELLED, str(result))


def t_cancelling_before_start_never_executes():
    router = CapabilityRouter()
    spec = _read_spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def _drive():
        token = asyncio.Event()
        token.set()
        return await router.execute(spec.name, {"entity": "x"}, trust=_user_turn(),
                                    cancel_token=token)

    result = _run(_drive())
    require_equal(result.outcome, CapabilityOutcome.CANCELLED, str(result))
    require(not ran, "trotz gesetztem Abbruch wurde ausgefuehrt")


def t_executor_unavailable_states_that_nothing_happened():
    router = CapabilityRouter()
    spec = _read_spec()

    def _down(_args):
        raise ExecutorUnavailable("Container aus")

    router.register(spec, _down)
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.EXECUTOR_UNAVAILABLE, str(result))
    require(result.had_no_effect, "executor_unavailable galt nicht als wirkungslos")


def t_provider_exceptions_never_reach_the_model():
    """Die Diagnose bleibt intern; das Modell bekommt einen stabilen Code."""
    router = CapabilityRouter()
    spec = _read_spec()

    def _boom(_args):
        raise RuntimeError("psycopg2.OperationalError: FATAL password authentication failed")

    router.register(spec, _boom)
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.CAPABILITY_FAILED, str(result))
    payload = result.as_dict()
    require("password" not in repr(payload),
            f"die rohe Provider-Meldung erreichte das Modell: {payload}")
    require("detail" not in payload, "detail gehoert nicht in die Modellsicht")
    require("password" in result.detail, "die Diagnose ging intern verloren")


def t_the_envelope_maps_back_to_the_old_tool_result():
    ok = CapabilityResult(CapabilityOutcome.SUCCESS, "c-1", "x", data={"a": 1})
    require(ok.as_tool_result().success)
    bad = CapabilityResult(CapabilityOutcome.REJECTED_BY_POLICY, "c-1", "x",
                           reason="untrusted_origin")
    legacy = bad.as_tool_result()
    require(not legacy.success)
    require_equal(legacy.error, "rejected_by_policy:untrusted_origin")


def t_no_effect_outcomes_are_exactly_the_safe_ones():
    """Timeout, Abbruch und Recovery duerfen NIE als „nichts passiert" gelten."""
    for outcome in (CapabilityOutcome.TIMEOUT, CapabilityOutcome.CANCELLED,
                    CapabilityOutcome.RECOVERY_REQUIRED, CapabilityOutcome.SUCCESS):
        result = CapabilityResult(outcome, "c-1", "x")
        require(not result.had_no_effect,
                f"{outcome.value} behauptete faelschlich Wirkungslosigkeit")
    for outcome in (CapabilityOutcome.REJECTED_BY_POLICY, CapabilityOutcome.INVALID_INPUT,
                    CapabilityOutcome.APPROVAL_REQUIRED,
                    CapabilityOutcome.EXECUTOR_UNAVAILABLE):
        require(CapabilityResult(outcome, "c-1", "x").had_no_effect, outcome.value)


# =====================================================================
# Observability
# =====================================================================

def t_every_call_emits_an_ordered_lifecycle():
    events = []
    router = CapabilityRouter(recorder=events.append)
    spec = _read_spec()
    router.register(spec, lambda a: {"ok": True})
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    phases = [e.phase for e in events]
    require_equal(phases, ["requested", "started", "finished"], str(phases))
    require_equal([e.sequence for e in events], [1, 2, 3], "die Sequenz war nicht monoton")
    require(all(e.call_id == result.call_id for e in events),
            "die Ereignisse gehoerten nicht zum selben Aufruf")


def t_a_refusal_is_observable_too():
    events = []
    router = CapabilityRouter(approvals=_approved_broker(), recorder=events.append)
    spec = _write_spec()
    router.register(spec, lambda a: None)
    _run(router.execute(spec.name, {"entity": "x"}, trust=_email_turn()))
    require_equal([e.phase for e in events], ["requested", "rejected"])
    require_equal(events[-1].detail, "untrusted_origin", str(events[-1]))


def t_sequences_are_unique_across_calls():
    events = []
    router = CapabilityRouter(recorder=events.append)
    spec = _read_spec()
    router.register(spec, lambda a: {"ok": True})
    for _ in range(3):
        _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    sequences = [e.sequence for e in events]
    require_equal(sequences, sorted(sequences), "die Sequenz war nicht aufsteigend")
    require_equal(len(set(sequences)), len(sequences), "eine Sequenznummer kam doppelt vor")


def t_a_broken_recorder_never_breaks_execution():
    """Beobachtung darf die Sache nicht kaputtmachen, die sie beobachtet."""
    def _explode(_event):
        raise RuntimeError("Recorder kaputt")

    router = CapabilityRouter(recorder=_explode)
    spec = _read_spec()
    router.register(spec, lambda a: {"ok": True})
    result = _run(router.execute(spec.name, {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))


# =====================================================================
# Rueckwaertskompatibilitaet
# =====================================================================

class _FakeTool:
    name = "fake_read"
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, result=None):
        self._result = result or ToolResult(True, data={"aus": "tool"})
        self.calls = []

    def schema(self):
        return {"type": "function", "name": self.name, "description": "",
                "parameters": {"type": "object",
                               "properties": {"entity": {"type": "string"}},
                               "required": ["entity"]}}

    async def run(self, arguments):
        self.calls.append(arguments)
        return self._result


def t_an_existing_tool_runs_unchanged_under_the_contract():
    tool = _FakeTool()
    spec = spec_for_tool(tool)
    require_equal(spec.name, "fake_read")
    require(spec.is_read_only(), "ein harmloses Werkzeug wurde nicht als Lesen erkannt")
    router = CapabilityRouter()
    router.register(spec, handler_for_tool(tool))
    result = _run(router.execute("fake_read", {"entity": "licht"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(result.data, {"aus": "tool"})
    require_equal(len(tool.calls), 1)


def t_an_unclassified_mutating_tool_defaults_to_the_safe_semantics():
    """Vergessene Klassifizierung kostet Sicherheit, nicht Stille."""
    class _Risky(_FakeTool):
        name = "fake_write"
        risk_level = RiskLevel.MUTATING

    spec = spec_for_tool(_Risky())
    require_equal(spec.semantics, NON_IDEMPOTENT_WRITE)
    require(spec.execution_class is ExecutionClass.CONTROLLED, spec.execution_class)


def t_a_failing_legacy_tool_becomes_a_named_failure():
    tool = _FakeTool(result=ToolResult(False, error="ha_unreachable"))
    router = CapabilityRouter()
    router.register(spec_for_tool(tool), handler_for_tool(tool))
    result = _run(router.execute("fake_read", {"entity": "x"}, trust=_user_turn()))
    require_equal(result.outcome, CapabilityOutcome.CAPABILITY_FAILED, str(result))
    require("ha_unreachable" in result.detail, result.detail)


def _require_core_principal_binding(source):
    """Die eine Core-Turnnaht: Auth-Principal oder ausdruecklicher Entzug."""
    method = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_tool_calls")
    assignments = sorted((n for n in ast.walk(method) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "turn_principal"
                                  for t in n.targets)), key=lambda n:n.lineno)
    require_equal([ast.unparse(n.value) for n in assignments], ["self.satellite_id", "''"],
                  "Principal stammt nicht ausschliesslich aus Auth oder wird neu erweitert")
    denial = next((n for n in ast.walk(method) if isinstance(n, ast.If)
                   and assignments[1] in n.body), None)
    require(denial is not None and ast.unparse(denial.test) ==
            "self.channel == 'voice_browser' and turn_origin is not OriginClass.TRUSTED_DASHBOARD",
            "ein Browser ohne lebenden Herkunftsbeleg muss sein Principal verlieren")
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "begin_turn"]
    require_equal(len(calls), 1)
    principal = next(k.value for k in calls[0].keywords if k.arg == "principal")
    require_equal(ast.unparse(principal), "turn_principal", "Modellargument ersetzt Auth-Principal")
    require(assignments[0].lineno < denial.lineno < calls[0].lineno,
            "Principal-Entzug erfolgt erst nach Aufrufkontext")


def t_the_dispatcher_stays_generic_and_the_core_only_supplies_context():
    """Wo der Vertrag haengt — und wo ausdruecklich nicht.

    In der Vertragsstufe hing er nirgends im Sprachweg. Mit der ersten echten
    Faehigkeit (Home Assistant) setzt der Realtime-Core den vertrauenswuerdigen
    Aufrufkontext; das ist der Zweck jenes Milestones und wird hier festgehalten,
    damit es eine bewusste Entscheidung bleibt und keine Nebenwirkung.

    Der Dispatcher selbst bleibt unwissend: er kennt Werkzeuge, keine Faehigkeiten.
    Und das Principal kommt weiter aus der Authentifizierung, nie aus Argumenten.
    """
    import inspect
    from solvio.realtime import core_server
    from solvio.tools import dispatcher as dispatcher_module
    require("capabilities" not in inspect.getsource(dispatcher_module),
            "der Dispatcher kennt jetzt Faehigkeiten — die Schichtung ist verrutscht")
    core = inspect.getsource(core_server)
    require("capability_gate" in core and "begin_turn" in core,
            "der Core setzt den vertrauenswuerdigen Aufrufkontext nicht mehr")
    _require_core_principal_binding(core)
    require("principal=args" not in core and 'principal=arguments' not in core,
            "das Principal wurde aus Argumenten gespeist")
    for before, after in (
        ("turn_principal = self.satellite_id", 'turn_principal = calls[0]["principal"]'),
        ('turn_principal = ""', 'turn_principal = self.satellite_id'),
        ("principal=turn_principal", 'principal=calls[0]["principal"]'),
    ):
        require(before in core)
        require_raises(AssertionError, _require_core_principal_binding, core.replace(before, after))


# =====================================================================
# Vertragsform
# =====================================================================

def t_a_spec_refuses_nonsense():
    require_raises(ValueError, CapabilitySpec, name="", version=1,
                   execution_class=ExecutionClass.FAST, base_risk=RiskLevel.HARMLESS,
                   semantics=READ_ONLY)
    require_raises(ValueError, CapabilitySpec, name="x", version=0,
                   execution_class=ExecutionClass.FAST, base_risk=RiskLevel.HARMLESS,
                   semantics=READ_ONLY)
    require_raises(ValueError, CapabilitySpec, name="x", version=1,
                   execution_class=ExecutionClass.FAST, base_risk=RiskLevel.HARMLESS,
                   semantics="erfunden")
    require_raises(ValueError, CapabilitySpec, name="x", version=1,
                   execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
                   semantics=NON_IDEMPOTENT_WRITE, timeout=0)


def t_all_four_execution_classes_are_describable():
    require_equal(sorted(c.value for c in ExecutionClass),
                  ["background", "controlled", "deep", "fast"])
    for cls in (ExecutionClass.CONTROLLED, ExecutionClass.DEEP, ExecutionClass.BACKGROUND):
        spec = _write_spec(name=f"probe_{cls.value}", execution_class=cls)
        require_equal(spec.execution_class, cls)


def t_a_spec_is_immutable():
    spec = _read_spec()
    err = require_raises(Exception, setattr, spec, "base_risk", RiskLevel.CRITICAL)
    require(err is not None)


def t_privacy_defaults_to_home_only():
    """Fail-closed: was nicht ausdruecklich reisen darf, bleibt zuhause."""
    from solvio.nodes.models import DataClass
    require_equal(_read_spec().data_class, DataClass.HOME_ONLY)


def t_duplicate_registration_is_refused():
    router = CapabilityRouter()
    spec = _read_spec()
    router.register(spec, lambda a: None)
    require_raises(ValueError, router.register, spec, lambda a: None)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
