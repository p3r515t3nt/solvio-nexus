"""Produktive Freigabe-Verdrahtung — Verhaltenstests ohne Netz und ohne iPhone.

Diese Suite prueft die **Naht**, nicht das eingefrorene Sicherheitssystem dahinter.
Die Fragen:

* Fuehrt eine Freigabe fuer Aktion A jemals Aktion B aus?
* Kann eine Freigabe zweimal wirken?
* Kann das Modell — oder fremder Inhalt — Autoritaet erzeugen?
* Sagt der Weg die Wahrheit, wenn der Ausgang unklar ist?
* Bleiben Lesevorgaenge frei von Rueckfragen?
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.approval_gateway import (  # noqa: E402
    ACTION_LABELS, CapabilityApprovals, approval_digest, approval_mode,
    labels_are_unambiguous, render_action,
)
from solvio.capabilities.contract import (  # noqa: E402
    ArgumentSource, CapabilitySpec, ExecutionClass,
)
from solvio.capabilities.envelope import CapabilityOutcome  # noqa: E402
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.invocation import (  # noqa: E402
    CapabilityInvocationGate, voice_trust,
)
from solvio.capabilities.router import CapabilityRouter  # noqa: E402
from solvio.contracts.trust import TrustContext, TrustLevel  # noqa: E402
from solvio.security.approval import action_digest  # noqa: E402
from solvio.security.mobile_approval import execution as X  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402
from solvio.tools.base import RiskLevel  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _spec(name="calendar_create_event", version=1, risk=RiskLevel.MUTATING):
    return CapabilitySpec(
        name=name, version=version, execution_class=ExecutionClass.CONTROLLED,
        base_risk=risk, semantics=X.IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "title": {"type": "string"}, "when": {"type": "string"},
            "time": {"type": "string"}}, "required": ["title"]})


# =====================================================================
# Ein Kontrollpfad aus Papier — mit denselben Regeln wie der echte
# =====================================================================

class _FakeStore:
    def __init__(self):
        self.requests = {}

    async def get_request(self, approval_id):
        return self.requests.get(approval_id)


class _FakeControlPlane:
    """Bildet die Regeln nach, auf die es hier ankommt — mehr nicht."""

    def __init__(self):
        self.store = _FakeStore()
        self.core_instance_id = "core-test"
        self._counter = 0

    async def create_request(self, *, principal, tool, mode, task, workspace,
                             human_summary):
        self._counter += 1
        approval_id = f"ap-{self._counter:04d}"
        self.store.requests[approval_id] = {
            "approval_id": approval_id, "principal": principal, "tool": tool,
            "mode": mode, "task": task, "workspace": workspace,
            "human_summary": human_summary, "state": S.PENDING,
            "action_digest": action_digest(tool_id=tool, mode=mode, task=task,
                                           workspace=workspace),
            "decided_device": None}
        return approval_id

    def approve(self, approval_id, device="dev-test"):
        """Steht fuer die verifizierte iPhone-Entscheidung."""
        self.store.requests[approval_id]["state"] = S.APPROVED
        self.store.requests[approval_id]["decided_device"] = device

    def expire(self, approval_id):
        self.store.requests[approval_id]["state"] = "EXPIRED"

    def deny(self, approval_id):
        self.store.requests[approval_id]["state"] = "DENIED"


class _FakeCoordinator:
    """Fuehrt nur aus, was freigegeben ist — und genau einmal."""

    def __init__(self, control_plane, *, ambiguous=False, safe_failure=False):
        self.cp = control_plane
        self.ambiguous = ambiguous
        self.safe_failure = safe_failure
        self.executions = []

    async def execute_approved(self, approval_id, executor):
        req = self.cp.store.requests.get(approval_id)
        if req is None or req["state"] != S.APPROVED:
            return None, "not_approved"
        # Einmalig: die Freigabe ist mit dem Anspruch verbraucht.
        req["state"] = S.CONSUMED
        execution_id = X.execution_id_for(self.cp.core_instance_id, approval_id)
        payload = {"tool": req["tool"], "mode": req["mode"], "task": req["task"],
                   "workspace": req["workspace"], "action_digest": req["action_digest"],
                   "execution_id": execution_id,
                   "idempotency_key": X.idempotency_key_for(execution_id, req["tool"]),
                   "semantics": X.semantics_for(req["tool"])}
        if self.safe_failure:
            return None, "failed_safe"
        if self.ambiguous:
            return None, "unknown_outcome"
        ok, info = await executor(payload)
        self.executions.append((approval_id, payload))
        return ({"info": info, "execution_id": execution_id}, "ok") if ok \
            else (None, "unknown_outcome")


def _stack(**kw):
    cp = _FakeControlPlane()
    coord = _FakeCoordinator(cp, **kw)
    approvals = CapabilityApprovals(coord, owner_principal="local-owner")
    router = CapabilityRouter(mobile=approvals)
    gate = CapabilityInvocationGate()
    return cp, coord, router, gate


def _turn(gate, said, *, principal="pi-wohnzimmer", trust=None,
          origin=OriginClass.ROOM_VOICE):
    """Der Satellit im Wohnzimmer — die Herkunft, unter der dieser Test steht."""
    gate.begin_turn(session_id="s", turn_id="t", principal=principal,
                    trust=trust or voice_trust(True), user_text=said,
                    origin=origin)
    return gate.context()


async def _call(router, gate, name, args, said, *, approval_id=None, trust=None):
    ctx = _turn(gate, said, trust=trust)
    return await router.execute(name, args, trust=ctx.trust,
                                provenance=gate.provenance_for(args),
                                principal=ctx.principal, approval_request_id=approval_id,
                                origin=ctx.origin, commanded=ctx.commanded)


# =====================================================================
# Was der Nutzer auf dem Display sieht
# =====================================================================

def t_the_approval_text_is_readable():
    spec = _spec()
    text = render_action(spec, {"title": "Zahnarzt", "when": "morgen", "time": "15:00"})
    require("Kalendertermin anlegen" in text, text)
    require("Titel: \"Zahnarzt\"" in text, text)
    require("Uhrzeit: \"15:00\"" in text, text)


def t_the_approval_text_determines_the_action():
    """Zwei verschiedene Aktionen duerfen nie denselben Text ergeben."""
    spec = _spec()
    seen = {}
    for title in ("Zahnarzt", "Zahnarzt "):
        for time_ in ("15:00", "16:00"):
            args = {"title": title, "time": time_}
            text = render_action(spec, args)
            require(text not in seen or seen[text] == args,
                    f"zwei Aktionen ergaben denselben Freigabetext: {text!r}")
            seen[text] = args
    require_equal(len(seen), 4, "der Text unterscheidet nicht genug")


def t_argument_order_does_not_change_the_text():
    spec = _spec()
    a = render_action(spec, {"title": "X", "time": "15:00"})
    b = render_action(spec, {"time": "15:00", "title": "X"})
    require_equal(a, b, "die Schluesselreihenfolge veraenderte den Freigabetext")


def t_labels_never_collide():
    require_equal(labels_are_unambiguous(), [],
                  "eine Faehigkeit hat zwei Argumente mit derselben Beschriftung")
    for name, (headline, labels) in ACTION_LABELS.items():
        require(headline.strip(), f"{name} hat keine Ueberschrift")


def t_the_version_is_part_of_the_binding():
    """Eine Freigabe fuer v1 gilt nicht fuer v2."""
    args = {"title": "X"}
    require(approval_digest(_spec(version=1), args)
            != approval_digest(_spec(version=2), args),
            "die Version aendert den Digest nicht")
    require("-v1" in approval_mode(_spec(version=1)))


def t_the_digest_matches_what_the_control_plane_stores():
    """Kein zweites Digest-Schema: derselbe Wert, denselben Weg gerechnet."""
    cp, _, _, _ = _stack()
    spec, args = _spec(), {"title": "Zahnarzt", "time": "15:00"}

    async def go():
        approvals = CapabilityApprovals(_FakeCoordinator(cp), owner_principal="local-owner")
        approval_id = await approvals.request(spec, args)
        return cp.store.requests[approval_id]["action_digest"]

    require_equal(_run(go()), approval_digest(spec, args))


# =====================================================================
# Bindung: freigegeben wird genau eine Aktion
# =====================================================================

def t_the_approved_action_executes():
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a) or {"created": True})
    args = {"title": "Zahnarzt", "when": "morgen", "time": "15:00"}

    async def go():
        first = await _call(router, gate, spec.name, args,
                            "Trag morgen um 15 Uhr Zahnarzt ein.")
        require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(first))
        require(not ran, "vor der Freigabe wurde ausgefuehrt")
        approval_id = first.data["request_id"]
        cp.approve(approval_id)
        return await _call(router, gate, spec.name, args,
                           "Trag morgen um 15 Uhr Zahnarzt ein.", approval_id=approval_id)

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(len(ran), 1, "die Aktion lief nicht genau einmal")
    require_equal(ran[0]["title"], "Zahnarzt")


def t_a_changed_argument_is_refused():
    """Freigegeben wurde 15 Uhr. Ausgefuehrt werden soll 16 Uhr."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        first = await _call(router, gate, spec.name,
                            {"title": "Zahnarzt", "time": "15:00"}, "Trag Zahnarzt ein.")
        approval_id = first.data["request_id"]
        cp.approve(approval_id)
        return await _call(router, gate, spec.name,
                           {"title": "Zahnarzt", "time": "16:00"}, "Trag Zahnarzt ein.",
                           approval_id=approval_id)

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "approval_drift", str(result))
    require(not ran, "eine andere Aktion lief unter einer erteilten Freigabe")


def t_a_changed_capability_is_refused():
    cp, coord, router, gate = _stack()
    create, delete = _spec(), _spec(name="calendar_delete_event",
                                    risk=RiskLevel.CRITICAL)
    ran = []
    router.register(create, lambda a: ran.append(("create", a)))
    router.register(delete, lambda a: ran.append(("delete", a)))
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, create.name, args, "Trag Zahnarzt ein.")
        approval_id = first.data["request_id"]
        cp.approve(approval_id)
        # Dieselbe Freigabe, andere Faehigkeit.
        return await _call(router, gate, delete.name, args, "Loesch Zahnarzt.",
                           approval_id=approval_id)

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require(result.reason in ("approval_drift", "approval_capability_mismatch"),
            str(result))
    require(not ran, "eine Freigabe wirkte fuer eine fremde Faehigkeit")


def t_an_approval_works_only_once():
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a) or {"ok": True})
    args = {"title": "Zahnarzt", "time": "15:00"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        approval_id = first.data["request_id"]
        cp.approve(approval_id)
        a = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                        approval_id=approval_id)
        b = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                        approval_id=approval_id)
        return a, b

    first, second = _run(go())
    require_equal(first.outcome, CapabilityOutcome.SUCCESS, str(first))
    require_equal(second.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(second))
    require_equal(second.reason, "not_approved", str(second))
    require_equal(len(ran), 1, "die Freigabe wirkte zweimal")


def t_an_unapproved_id_executes_nothing():
    """Die Kennung allein ist keine Autoritaet."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        # KEINE Freigabe auf dem Geraet — nur die bekannte Kennung.
        return await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                           approval_id=first.data["request_id"])

    result = _run(go())
    require_equal(result.reason, "not_approved", str(result))
    require(not ran)


def t_a_denied_approval_executes_nothing():
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        cp.deny(first.data["request_id"])
        return await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                           approval_id=first.data["request_id"])

    # `denied` und nicht mehr `not_approved`: seit der Live-Abnahme von
    # DEBT-0126 (2026-08-30) wird eine ABGELEHNTE Freigabe von einer noch
    # unbeantworteten unterschieden. Der Nachbarfall `t_an_expired_approval_...`
    # prueft unveraendert `not_approved` — ein Fristablauf ist keine Antwort,
    # und genau diese Grenze soll das Paar festhalten.
    require_equal(_run(go()).reason, "denied")
    require(not ran, "eine abgelehnte Freigabe hat ausgefuehrt")


def t_an_expired_approval_executes_nothing():
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        cp.expire(first.data["request_id"])
        return await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                           approval_id=first.data["request_id"])

    require_equal(_run(go()).reason, "not_approved")
    require(not ran, "eine abgelaufene Freigabe hat ausgefuehrt")


def t_an_invented_approval_id_is_refused():
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        return await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.",
                           approval_id="ap-erfunden")

    require_equal(_run(go()).reason, "unknown_approval")
    require(not ran)


# =====================================================================
# Autoritaet
# =====================================================================

def t_the_model_cannot_approve_itself():
    """Es gibt keinen Weg vom Router oder der Bruecke zu einer Bestaetigung."""
    import inspect
    from solvio.capabilities import approval_gateway, router as router_module
    for module in (router_module, approval_gateway):
        source = inspect.getsource(module)
        for forbidden in (".approve(", ".confirm(", "submit_decision"):
            require(forbidden not in source,
                    f"{module.__name__} enthaelt {forbidden} — Autoritaet gehoert dem Geraet")


def t_approval_arguments_from_the_model_are_no_authority():
    """`confirmed`/`approved` im Argumentsatz bleiben wirkungslos."""
    cp, coord, router, gate = _stack()
    spec = CapabilitySpec(
        name="calendar_create_event", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=X.IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "title": {"type": "string"}, "confirmed": {"type": "boolean"},
            "approved": {"type": "boolean"}}, "required": ["title"]})
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        return await _call(router, gate, spec.name,
                           {"title": "X", "confirmed": True, "approved": True},
                           "Trag X ein.")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))
    require(not ran, "ein Modellargument hat die Freigabe ersetzt")


def t_untrusted_content_never_reaches_the_approval_path():
    """Fremder Inhalt bekommt nicht einmal eine Freigabefrage."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        return await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.",
                           trust=TrustContext(origin_trust=TrustLevel.UNTRUSTED_EMAIL,
                                              user_authorized=True))

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "untrusted_origin", str(result))
    require_equal(len(cp.store.requests), 0,
                  "fremder Inhalt hat eine Freigabeanfrage auf dem iPhone erzeugt")
    require(not ran)


def t_a_model_generated_turn_creates_no_approval():
    cp, coord, router, gate = _stack()
    spec = _spec()
    router.register(spec, lambda a: None)

    async def go():
        return await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.",
                           trust=TrustContext(origin_trust=TrustLevel.AGENT_GENERATED,
                                              user_authorized=True))

    require_equal(_run(go()).reason, "no_user_authority")
    require_equal(len(cp.store.requests), 0)


def t_the_approval_request_is_addressed_to_the_device_owner():
    """Wer fragt und wer freigeben darf, sind zwei Rollen."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    router.register(spec, lambda a: None)

    async def go():
        await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.")
        return list(cp.store.requests.values())[0]

    stored = _run(go())
    require_equal(stored["principal"], "local-owner",
                  "die Anfrage ging nicht an den registrierten Besitzer")
    require("pi-wohnzimmer" not in stored["task"],
            "das Aufruf-Principal steht im autorisierenden Text")


def t_the_human_summary_carries_no_action_claim():
    """Protokollseitig gilt es als unvertrauenswuerdig — also steht dort nichts Wichtiges."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    router.register(spec, lambda a: None)

    async def go():
        await _call(router, gate, spec.name, {"title": "Zahnarzt"}, "Trag Zahnarzt ein.")
        return list(cp.store.requests.values())[0]

    stored = _run(go())
    require("Zahnarzt" not in stored["human_summary"],
            "die Aktion steht im unvertrauenswuerdigen Feld")
    require("Zahnarzt" in stored["task"], "die Aktion fehlt im autorisierenden Feld")


# =====================================================================
# Ausgang
# =====================================================================

def t_an_ambiguous_outcome_demands_recovery():
    cp, coord, router, gate = _stack(ambiguous=True)
    spec = _spec()
    router.register(spec, lambda a: {"ok": True})
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        cp.approve(first.data["request_id"])
        return await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                           approval_id=first.data["request_id"])

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.RECOVERY_REQUIRED, str(result))
    require(not result.had_no_effect,
            "ein unklarer Ausgang wurde als wirkungslos gemeldet")


def t_a_safe_failure_states_that_nothing_happened():
    cp, coord, router, gate = _stack(safe_failure=True)
    spec = _spec()
    router.register(spec, lambda a: {"ok": True})
    args = {"title": "Zahnarzt"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        cp.approve(first.data["request_id"])
        return await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.",
                           approval_id=first.data["request_id"])

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.CAPABILITY_FAILED, str(result))
    require(result.had_no_effect)


# =====================================================================
# Regression: nicht alles wird zur Face-ID-Frage
# =====================================================================

def t_reads_never_ask_for_approval():
    cp, coord, router, gate = _stack()
    read = CapabilitySpec(name="calendar_list_events", version=1,
                          execution_class=ExecutionClass.FAST,
                          base_risk=RiskLevel.HARMLESS, semantics=X.READ_ONLY,
                          input_schema={"type": "object", "properties": {
                              "when": {"type": "string"}}})
    router.register(read, lambda a: {"count": 0})

    async def go():
        return await _call(router, gate, read.name, {"when": "morgen"},
                           "Was habe ich morgen im Kalender?")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(len(cp.store.requests), 0, "ein Lesevorgang erzeugte eine Freigabe")


def t_a_harmless_control_stays_direct():
    """Licht an bleibt Licht an — die Verdrahtung hebt kein Risiko an."""
    cp, coord, router, gate = _stack()
    light = CapabilitySpec(name="ha_turn_on", version=1,
                           execution_class=ExecutionClass.CONTROLLED,
                           base_risk=RiskLevel.HARMLESS, semantics=X.IDEMPOTENT_WRITE,
                           input_schema={"type": "object", "properties": {
                               "name": {"type": "string"}}, "required": ["name"]})
    ran = []
    router.register(light, lambda a: ran.append(a) or {"state": "on"})

    async def go():
        return await _call(router, gate, light.name, {"name": "Flur Licht"},
                           "Mach das Flur Licht an.")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(len(cp.store.requests), 0, "eine harmlose Schaltung fragte nach Face ID")
    require_equal(len(ran), 1)


def t_a_security_sensitive_action_would_enter_the_approval_path():
    """Trockenlauf: ein sicherheitsrelevantes Geraet landet auf dem iPhone."""
    cp, coord, router, gate = _stack()
    lock = CapabilitySpec(name="ha_turn_off", version=1,
                          execution_class=ExecutionClass.CONTROLLED,
                          base_risk=RiskLevel.CRITICAL, semantics=X.NON_IDEMPOTENT_WRITE,
                          input_schema={"type": "object", "properties": {
                              "name": {"type": "string"}}, "required": ["name"]})
    ran = []
    router.register(lock, lambda a: ran.append(a))

    async def go():
        return await _call(router, gate, lock.name, {"name": "Alarmanlage"},
                           "Schalt die Alarmanlage aus.")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))
    require_equal(len(cp.store.requests), 1, "die Aktion ging nicht auf das Geraet")
    require(not ran, "ein sicherheitsrelevantes Geraet wurde ohne Freigabe geschaltet")
    stored = list(cp.store.requests.values())[0]
    require("Alarmanlage" in stored["task"], "das Ziel fehlte im Freigabetext")


def t_a_model_derived_argument_escalates_into_the_approval_path():
    """Erwaehnung ist keine Ermaechtigung — auch hier nicht."""
    cp, coord, router, gate = _stack()
    light = CapabilitySpec(name="ha_turn_on", version=1,
                           execution_class=ExecutionClass.CONTROLLED,
                           base_risk=RiskLevel.HARMLESS, semantics=X.IDEMPOTENT_WRITE,
                           input_schema={"type": "object", "properties": {
                               "name": {"type": "string"}}, "required": ["name"]})
    ran = []
    router.register(light, lambda a: ran.append(a))

    async def go():
        return await _call(router, gate, light.name, {"name": "Flur Licht"},
                           "Was steht in der Mail?")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))
    require(not ran)


def t_without_a_device_path_writes_still_fail_closed():
    """Ohne verdrahteten Freigabeweg passiert nichts Wirksames."""
    from solvio.security.approval import ApprovalBroker
    router = CapabilityRouter(approvals=ApprovalBroker())   # kein Approver, kein Geraet
    gate = CapabilityInvocationGate()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        first = await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.")
        return await _call(router, gate, spec.name, {"title": "X"}, "Trag X ein.",
                           approval_id=first.data["request_id"])

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require(not ran, "ohne Freigabeweg wurde ausgefuehrt")


def t_the_frozen_semantics_govern_the_execution_identity():
    """Der eingefrorene Pfad bleibt die Autoritaet ueber Wiederholbarkeit."""
    require_equal(X.semantics_for("calendar_create_event"), X.NON_IDEMPOTENT_WRITE,
                  "eine unklassifizierte Faehigkeit gilt nicht mehr als fail-safe")
    require_equal(X.recovery_decision(X.EXTERNAL_PENDING, X.NON_IDEMPOTENT_WRITE),
                  X.MANUAL_RECOVERY_REQUIRED)


# =====================================================================
# Der gesprochene Weg: es gibt keine Kennung, die das Modell nennen koennte
# =====================================================================

def t_a_spoken_retry_finds_the_approval_the_human_granted():
    """Live gescheitert: freigegeben, und trotzdem ist nie etwas passiert.

    Ueber den Sprachpfad hat das Modell KEINE Moeglichkeit, eine
    Freigabekennung zu nennen — im Werkzeugschema gibt es keine, und der
    Dispatcher reicht auch keine durch. Der Nutzer sagte „trag mir einen Termin
    ein", bestaetigte auf dem iPhone mit Face ID und sagte SOLVIO Bescheid. Das
    Modell rief die Faehigkeit erneut auf, die eben freigegebene Anfrage wurde
    dabei verworfen, und der Termin entstand nie.
    """
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a) or {"created": True})
    args = {"title": "Zahnarzt", "when": "morgen", "time": "15:00"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag morgen um 15 Uhr Zahnarzt ein.")
        require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(first))
        cp.approve(first.data["request_id"])          # Face ID auf dem Geraet
        # Und jetzt genau das, was der Sprachpfad kann: nochmal fragen, ohne Kennung.
        return await _call(router, gate, spec.name, args, "Ich habe freigegeben.")

    result = _run(go())
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(len(ran), 1, "die freigegebene Handlung lief nicht genau einmal")
    require_equal(ran[0]["title"], "Zahnarzt")


def t_a_spoken_retry_with_a_different_action_never_rides_the_old_approval():
    """Freigegeben wurde 15 Uhr. Gesagt wird jetzt 16 Uhr — ohne Kennung.

    Das ist die Stelle, an der ein bequemes Fortsetzen zur Luecke wuerde: die
    alte Freigabe liegt bereit, die neue Handlung ist eine andere. Sie darf
    nicht darauf reiten, und der Mensch muss sie neu sehen.
    """
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))

    async def go():
        first = await _call(router, gate, spec.name,
                            {"title": "Zahnarzt", "time": "15:00"}, "Trag Zahnarzt ein.")
        cp.approve(first.data["request_id"])
        return await _call(router, gate, spec.name,
                           {"title": "Zahnarzt", "time": "16:00"}, "Doch lieber 16 Uhr.")

    result = _run(go())
    require(not ran, "eine andere Handlung lief unter einer erteilten Freigabe")
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))
    require(result.data["request_id"], "es wurde keine neue Freigabe angefragt")


def t_a_retry_before_the_human_decided_still_only_asks():
    """Ungeduld ist keine Zustimmung. Zweimal fragen fuehrt nichts aus."""
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"title": "Zahnarzt", "time": "15:00"}

    async def go():
        await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        return await _call(router, gate, spec.name, args, "Und jetzt?")

    result = _run(go())
    require(not ran, "ohne Entscheidung des Menschen wurde ausgefuehrt")
    require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(result))


def t_a_denied_approval_is_never_resumed_by_a_retry():
    """Eine abgelehnte Freigabe ist endgueltig — auch fuer den naechsten Anlauf.

    Ein Modell sagt „Ich habe freigegeben", nachdem der Mensch abgelehnt hat.
    Getragen hat dieser Fall immer die eine Zusicherung, auf die es ankommt:
    **es wird nichts ausgefuehrt.** Die zweite Zeile prueft, WAS statt dessen
    geschieht — und da stand bis zur Live-Abnahme von DEBT-0126 (2026-08-30)
    `APPROVAL_REQUIRED`: der Router verwarf die Ablehnung und fragte NEU.

    So war das Nachfragen als erwartetes Verhalten festgeschrieben, und deshalb
    hat es keine der 2788 Zusicherungen gemeldet. Am Geraet sah es so aus: der
    Eigentuemer lehnte eine Kauffreigabe ab und bekam sofort dieselbe Karte
    zurueck, zweimal hintereinander.

    Ehrlich zum Preis: nach einer VERSEHENTLICHEN Ablehnung kostet es einen
    Satz mehr. Der erste Anlauf bekommt „Das hast du abgelehnt. Dabei bleibt
    es."; erst der naechste erzeugt wieder eine Karte. Das ist die richtige
    Reihenfolge — ein Mensch soll hoeren, dass sein Nein angekommen ist, bevor
    er noch einmal gefragt wird.
    """
    cp, coord, router, gate = _stack()
    spec = _spec()
    ran = []
    router.register(spec, lambda a: ran.append(a))
    args = {"title": "Zahnarzt", "time": "15:00"}

    async def go():
        first = await _call(router, gate, spec.name, args, "Trag Zahnarzt ein.")
        cp.deny(first.data["request_id"])
        return await _call(router, gate, spec.name, args, "Ich habe freigegeben.")

    result = _run(go())
    require(not ran, "eine abgelehnte Freigabe wurde fortgesetzt")
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "denied", str(result.reason))
    require_equal(len(cp.store.requests), 1,
                  "die Ablehnung hat eine neue Freigabekarte erzeugt — der "
                  "Mensch wird gefragt, bis er ja sagt")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
