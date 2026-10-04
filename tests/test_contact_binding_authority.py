"""Contact Binding Authority Hardening V1 — die Schliessung von DEBT-0208.

Eine bestaetigte Kontaktbindung sagt, welcher Mensch und welcher Kontaktweg
hinter „mich", „mein Sohn" oder „das Hotel" steht. Das ist Identitaetswahrheit.
Gemessen vor diesem Milestone: vom entsperrten iPhone liess sie sich ohne
Face ID umhaengen (`TRUSTED_INTERACTIVE_APP x CRITICAL = EXECUTE_DIRECTLY`).

Diese Zusicherungen laufen — wo es um Freigabe geht — ueber den ECHTEN
eingefrorenen Freigabepfad: Kontrollebene, Speicher, Geraetezuweisung,
signierte Entscheidung, Einmaligkeit. Keine Attrappe davon.

Alle Kontaktwege hier sind erkennbar erfunden: Rufnummern aus dem von Ofcom
fuer Fiktion reservierten Bereich +44 7700 900xxx, Adressen unter
`example.test`, Namen aus dem Musterkatalog.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H  # noqa: E402
from solvio.capabilities import execution_identity as EI  # noqa: E402
from solvio.capabilities import policy as AP  # noqa: E402
from solvio.capabilities.approval_gateway import (  # noqa: E402
    CapabilityApprovals, approval_digest)
from solvio.capabilities.communication import (  # noqa: E402
    SPECS, CommunicationCapabilities, register)
from solvio.capabilities.contract import ArgumentSource  # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome  # noqa: E402
from solvio.capabilities.invocation import (  # noqa: E402
    CapabilityInvocationGate, voice_trust)
from solvio.capabilities.router import CapabilityRouter  # noqa: E402
from solvio.communication.bindings import (  # noqa: E402
    ABSENT, BindingChanged, BindingStore, fingerprint)
from solvio.contracts.trust import TrustContext, TrustLevel  # noqa: E402
from solvio.secret_vault import context as SC  # noqa: E402
from solvio.security.approval import ApprovalBroker  # noqa: E402
from solvio.security.mobile_approval import bridge as B  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402
from solvio.security.mobile_approval.execution import SafeExecutionFailure  # noqa: E402
from solvio.tools.communication_capability_tools import _SCHEMAS  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
OWNER = "local-owner"
NAME = "communication_confirm_binding"

ALT = "+447700900111"
NEU = "+447700900222"
FREMD = "+447700900333"
DRITTE = "+447700900444"
IPHONE = AP.OriginClass.TRUSTED_INTERACTIVE_APP


class _Gmail:
    async def search(self, arguments):
        return {"messages": [], "content_trust": "untrusted_email"}


class _Stack:
    """Router + ECHTER Freigabepfad + Bindungsspeicher — je Fall neu."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="solvio-binding-case-")
        self.store = BindingStore(os.path.join(self.tmp, "contacts.sqlite3"))
        self.cap = CommunicationCapabilities(_Gmail(), self.store)
        self.gate = CapabilityInvocationGate()
        self.counter = 0
        self.st = self.cp = self.co = self.ctx = self.approvals = self.router = None

    async def start(self) -> "_Stack":
        self.st = S.ApprovalControlStore(os.path.join(self.tmp, "approval_control.sqlite3"))
        await self.st.open()
        self.cp = C.MobileApprovalControlPlane(
            self.st, identity.MacSigningKey.load_or_create(self.tmp),
            identity.load_or_create_core_instance_id(self.tmp),
            attest_verifier=H.fake_verifier(), app_id=APP_ID,
            allowed_environments={"development"})
        approver = B.MobileApprover()
        self.co = B.MobileApprovalCoordinator(
            self.cp, ApprovalBroker(approver=approver), approver)
        self.ctx = await H.enroll_attested(self.cp, principal=OWNER)
        self.approvals = CapabilityApprovals(self.co, owner_principal=OWNER)
        self.router = CapabilityRouter(mobile=self.approvals, policy_mode="enforce")
        register(self.router, self.cap)
        return self

    # -- das Geraet entscheidet -------------------------------------------
    async def decide(self, approval_id: str, decision=P.DECISION_APPROVE) -> str:
        wire, status = await self.cp.issue_challenge(approval_id=approval_id,
                                                     device_id=self.ctx.device_id)
        require_equal(status, "ok", f"challenge: {status}")
        self.counter += 1
        _, status = await self.co.apply_mobile_decision(**H.sign_decision(
            self.ctx, P.b64d(wire["payload_b64"]), decision=decision,
            counter=self.counter))
        return status

    async def state(self, approval_id: str) -> str:
        row = await self.st.get_request(approval_id)
        return str((row or {}).get("state") or "WEG")

    async def requests(self) -> list[dict]:
        con = sqlite3.connect(os.path.join(self.tmp, "approval_control.sqlite3"))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(
                "SELECT approval_id, state, task FROM approval_requests")]
        finally:
            con.close()

    # -- ein Aufruf, wie ihn die Welt stellt --------------------------------
    async def call(self, args: dict, *, origin=IPHONE, approval_id=None,
                   trust: TrustContext | None = None, provenance=None,
                   commanded: bool = True):
        self.gate.begin_turn(session_id="s", turn_id="t", principal="iphone",
                             origin=origin, commanded=commanded,
                             trust=trust or voice_trust(True),
                             user_text="Merk dir, wer mein Sohn ist.")
        context = self.gate.context()
        return await self.router.execute(
            NAME, args, trust=context.trust,
            provenance=(provenance if provenance is not None
                        else self.gate.provenance_for(args)),
            principal=context.principal, approval_request_id=approval_id,
            origin=context.origin, commanded=context.commanded)


def _args(value: str = NEU, alias: str = "mein Sohn", name: str = "Alex Muster") -> dict:
    return {"alias": alias, "display_name": name,
            "handles": [{"channel": "phone", "value": value}],
            "source": "user_confirmed"}


def _seed(stack: _Stack, value: str = ALT, alias: str = "mein Sohn",
          name: str = "Alex Muster") -> dict:
    """Eine bestehende Bindung — so, wie sie vor diesem Aufruf schon stand."""
    return stack.store.confirm(alias, name, [{"channel": "phone", "value": value}],
                               "test_seed")


def _digest(shown: dict, origin=AP.OriginClass.UNSPECIFIED) -> str:
    return approval_digest(SPECS[NAME], shown, AP.origin_label(origin))


def _bound(action_digest: str = "", approval_id: str = "appr-t"):
    """Der freigegebene Vorgang, wie `CapabilityApprovals.resume` ihn setzt."""
    return EI.bound(EI.ExecutionIdentity(
        execution_id="exec-t", idempotency_key="idem-t",
        approval_id=approval_id, capability=NAME, action_digest=action_digest))


# =========================================================================
# A / B — Erste Bindung und Umbindung: starke Eigentuemer-Autoritaet
# =========================================================================
def t_a_first_binding_from_the_iphone_needs_face_id():
    """A. Der Fall aus DEBT-0208, umgekehrt: das entsperrte iPhone reicht nicht."""
    async def lauf():
        stack = await _Stack().start()
        ergebnis = await stack.call(_args())
        require_equal(ergebnis.outcome, CapabilityOutcome.APPROVAL_REQUIRED,
                      f"{ergebnis.outcome} {ergebnis.reason}")
        require(stack.store.get("mein Sohn") is None,
                "die Bindung entstand, bevor jemand freigegeben hat")
        offen = await stack.requests()
        require_equal(len(offen), 1, "keine Freigabeanfrage auf dem Weg zum Geraet")
        require_equal(offen[0]["state"], S.PENDING)
        # Der Mensch sieht, dass es keine bisherige Bindung gab — und die Nummer.
        require("noch nicht gemerkt" in offen[0]["task"], offen[0]["task"])
        require(NEU in offen[0]["task"], "die Rufnummer fehlt im Freigabetext")
        require("Alex Muster" in offen[0]["task"], "der Name fehlt im Freigabetext")
    asyncio.run(lauf())


def t_rebinding_an_alias_from_the_iphone_needs_face_id():
    """B. Umhaengen ist die Handlung, um die es geht — und sie zeigt ALT und NEU."""
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        ergebnis = await stack.call(_args(NEU))
        require_equal(ergebnis.outcome, CapabilityOutcome.APPROVAL_REQUIRED,
                      f"{ergebnis.outcome} {ergebnis.reason}")
        require_equal(stack.store.get("mein Sohn"), vorher,
                      "die Bindung wurde vor der Freigabe umgehaengt")
        text = (await stack.requests())[0]["task"]
        require(f'Bisher erreichbar über: "Telefon: {ALT}"' in text, text)
        require(f'Künftig erreichbar über: "Telefon: {NEU}"' in text, text)
        require('Stand: "1"' in text, "die Fassung der ersetzten Bindung fehlt")
        require("Angefragt über: iPhone-App" in text, "die Herkunft fehlt")
    asyncio.run(lauf())


def t_every_origin_needs_face_id_or_is_denied():
    """Die Matrix, Zeile fuer Zeile: nirgends laeuft eine Bindung direkt."""
    klasse = AP.base_class(NAME, read_only=False)
    require_equal(klasse, AP.ActionClass.VERY_CRITICAL, "die Bindung ist nicht geburtskritisch")
    require(NAME in AP.VERY_CRITICAL_BY_BIRTH, "die Bindung steht nicht in der Geburtsregel")
    require(NAME not in AP.ACTION_CLASS,
            "eine zweite, mildere Wahrheit ueber die Bindung steht in der Registry")
    erwartet = {
        AP.OriginClass.TRUSTED_INTERACTIVE_APP: AP.Decision.REQUIRE_FACE_ID,
        AP.OriginClass.ROOM_VOICE: AP.Decision.REQUIRE_FACE_ID,
        AP.OriginClass.LOCAL_OWNER: AP.Decision.REQUIRE_FACE_ID,
        AP.OriginClass.BACKGROUND_AUTOMATION: AP.Decision.DENY,
        AP.OriginClass.EXTERNAL_UNTRUSTED: AP.Decision.DENY,
        AP.OriginClass.UNSPECIFIED: AP.Decision.REQUIRE_FACE_ID,
    }
    for origin, decision in erwartet.items():
        require_equal(AP.decide(origin, klasse, capability=NAME).decision, decision,
                      f"aus {origin.value}")


# =========================================================================
# C / D — Hintergrund und fremder Inhalt: DENY, nicht Face ID
# =========================================================================
def t_a_schedule_can_never_change_a_binding():
    """C. Ein Zeitplan hat keinen legitimen Grund, umzubinden — auch nicht per Frage."""
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        vertrauen = TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
                                 note="background task created by the user")
        ergebnis = await stack.call(
            _args(NEU), origin=AP.OriginClass.BACKGROUND_AUTOMATION, trust=vertrauen,
            provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in _args()})
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "policy_denied", ergebnis.reason)
        require_equal(await stack.requests(), [], "ein Zeitplan bekam eine Freigabefrage")
        require_equal(stack.store.get("mein Sohn"), vorher, "ein Zeitplan hat umgebunden")
    asyncio.run(lauf())


def t_foreign_content_can_never_change_a_binding():
    """D. Eine Mail, eine Webseite, ein Transkript: informieren ja, umbinden nie."""
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        # Der Turn selbst stammt aus fremdem Inhalt.
        fremd = TrustContext(origin_trust=TrustLevel.UNTRUSTED_EMAIL, user_authorized=True)
        ergebnis = await stack.call(_args(FREMD), origin=IPHONE, trust=fremd)
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "untrusted_origin", ergebnis.reason)
        # Die Herkunftsklasse selbst.
        ergebnis = await stack.call(_args(FREMD), origin=AP.OriginClass.EXTERNAL_UNTRUSTED)
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "policy_denied", ergebnis.reason)
        # Und ein einzelnes Argument aus fremdem Inhalt, vom iPhone aus: keine
        # Direktausfuehrung — der Mensch sieht die Nummer und entscheidet.
        ergebnis = await stack.call(
            _args(FREMD), origin=IPHONE,
            provenance={"alias": ArgumentSource.USER_DIRECT,
                        "display_name": ArgumentSource.USER_DIRECT,
                        "handles": ArgumentSource.UNTRUSTED_CONTENT,
                        "source": ArgumentSource.MODEL_DERIVED})
        require_equal(ergebnis.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(ergebnis))
        require_equal(stack.store.get("mein Sohn"), vorher, "fremder Inhalt hat umgebunden")
    asyncio.run(lauf())


# =========================================================================
# E — Abgelehnt oder abgelaufen: keinerlei Mutation
# =========================================================================
def t_a_denied_approval_writes_nothing_and_is_final():
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        erste = await stack.call(_args(NEU))
        aid = erste.data["request_id"]
        require_equal(await stack.decide(aid, P.DECISION_DENY), "ok")
        require_equal(await stack.state(aid), S.DENIED)

        ergebnis = await stack.call(_args(NEU), approval_id=aid)
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "denied", ergebnis.reason)
        require_equal(stack.store.get("mein Sohn"), vorher, "trotz Ablehnung umgebunden")
        # Und die Ablehnung wurde nicht durch eine neue Frage uebermalt.
        require_equal(len(await stack.requests()), 1, "nach einem Nein wurde neu gefragt")
    asyncio.run(lauf())


def t_an_expired_approval_writes_nothing():
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        erste = await stack.call(_args(NEU))
        aid = erste.data["request_id"]
        require(await stack.approvals.abandon(aid, reason="test_expiry"),
                "die Anfrage liess sich nicht ablaufen lassen")
        require_equal(await stack.state(aid), S.EXPIRED)

        ergebnis = await stack.call(_args(NEU), approval_id=aid)
        require(ergebnis.outcome is not CapabilityOutcome.SUCCESS, str(ergebnis))
        require_equal(stack.store.get("mein Sohn"), vorher, "trotz Ablauf umgebunden")
    asyncio.run(lauf())


# =========================================================================
# F — Zwischen Freigabe und Ausfuehrung aendert sich die Welt: fail-closed
# =========================================================================
def t_a_binding_changed_after_approval_is_not_written():
    """F. Erste Schranke: der Freigabetext wird beim Fortsetzen neu gebildet."""
    async def lauf():
        stack = await _Stack().start()
        _seed(stack, ALT)
        erste = await stack.call(_args(NEU))
        aid = erste.data["request_id"]
        require_equal(await stack.decide(aid), "ok")
        require_equal(await stack.state(aid), S.APPROVED)

        # Ein anderer, legitimer Schreiber haengt inzwischen um.
        dazwischen = stack.store.confirm("mein Sohn", "Alex Muster",
                                         [{"channel": "phone", "value": FREMD}], "other")
        ergebnis = await stack.call(_args(NEU), approval_id=aid)
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "approval_drift", ergebnis.reason)
        require_equal(stack.store.get("mein Sohn"), dazwischen,
                      "die Freigabe fuer den alten Stand schrieb ueber den neuen")
    asyncio.run(lauf())


def t_the_handler_refuses_a_stand_the_owner_did_not_see():
    """F, zweite Schranke: selbst NACH der letzten Beschreibung wird nicht geschrieben.

    Zwischen `_describe` und dem Handler liegt der Freigabepfad mit mehreren
    Wartepunkten. Aendert dort jemand die Bindung, passt der Text aus Wunsch
    und jetzigem Stand nicht mehr zum signierten Digest — und der Handler
    schreibt nicht. Kein Zustand dazwischen, gegen den man schreiben koennte.
    """
    stack = _Stack()
    _seed(stack, ALT)
    args = _args(NEU)
    freigegeben = _digest(stack.cap.describe_confirm_binding(args))
    dazwischen = stack.store.confirm("mein Sohn", "Alex Muster",
                                     [{"channel": "phone", "value": FREMD}], "other")
    # Ein dritter Beschreiber desselben Alias — frueher ueberschrieb er das
    # geteilte Fach und der Handler schrieb dann doch (Befund des Technical
    # Lead). Heute gibt es kein Fach.
    stack.cap.describe_confirm_binding(_args(DRITTE))
    with _bound(freigegeben):
        try:
            asyncio.run(stack.cap.confirm_binding(args))
            require(False, "ueber einen fremden Stand hinweg geschrieben")
        except SafeExecutionFailure as exc:
            require_equal(str(exc), "binding_changed_after_approval")
    require_equal(stack.store.get("mein Sohn"), dazwischen)


def t_a_writer_in_the_approval_window_cannot_smuggle_its_stand():
    """Die Sequenz des Technical Lead, am echten Freigabepfad.

    Freigabe fuer ALT -> NEU (Text: Bisher ALT, Stand 1). Der Router beschreibt
    beim Fortsetzen neu, dann wartet er im eingefrorenen Pfad. In diesem
    Fenster schreibt jemand FREMD (Fassung 2), und ein dritter Beschreiber
    desselben Alias laeuft. Erwartet: nichts wird geschrieben, die Freigabe
    endet als sicherer Fehlschlag, im Speicher steht FREMD.
    """
    async def lauf():
        stack = await _Stack().start()
        _seed(stack, ALT)
        erste = await stack.call(_args(NEU))
        aid = erste.data["request_id"]
        require_equal(await stack.decide(aid), "ok")

        echt = stack.co.execute_approved

        async def im_fenster(approval_id, executor):
            # Nach dem erneuten Beschreiben des Routers, vor dem Handler.
            stack.store.confirm("mein Sohn", "Alex Muster",
                                [{"channel": "phone", "value": FREMD}], "other")
            stack.cap.describe_confirm_binding(_args(DRITTE))
            return await echt(approval_id, executor)

        stack.co.execute_approved = im_fenster
        ergebnis = await stack.call(_args(NEU), approval_id=aid)
        require(ergebnis.outcome is not CapabilityOutcome.SUCCESS, str(ergebnis))
        require_equal(ergebnis.reason, "failed_safe", str(ergebnis))
        jetzt = stack.store.get("mein Sohn")
        require_equal(jetzt["handles"], [{"channel": "phone", "value": FREMD}])
        require_equal(jetzt["version"], 2, "im Fenster wurde doch geschrieben")
        require(await stack.state(aid) in (S.FAILED, S.CONSUMED),
                f"die Freigabe blieb ausfuehrbar: {await stack.state(aid)}")
        # Und mit derselben Kennung noch einmal: nichts.
        wieder = await stack.call(_args(NEU), approval_id=aid)
        require(wieder.outcome is not CapabilityOutcome.SUCCESS, str(wieder))
        require_equal(stack.store.get("mein Sohn"), jetzt)
    asyncio.run(lauf())


def t_two_argument_sets_never_share_one_approval_text():
    """Freigegeben wird der Text, ausgefuehrt werden die Argumente — beides eins.

    Zwei Wege `[Telefon X, E-Mail Y]` und EIN Weg mit dem Wert „X · E-Mail: Y"
    ergaben denselben Text und denselben Digest (Befund des Technical Lead).
    """
    zwei = dict(_args(NEU), handles=[{"channel": "phone", "value": NEU},
                                     {"channel": "email", "value": "alex@example.test"}])
    einer = dict(_args(NEU), handles=[{"channel": "phone",
                                       "value": f"{NEU} · E-Mail: alex@example.test"}])
    stack = _Stack()
    a, b = stack.cap.describe_confirm_binding(zwei), stack.cap.describe_confirm_binding(einer)
    require(a != b, "zwei Argumentmengen, eine Anzeige")
    require(_digest(a) != _digest(b), "zwei Argumentmengen, ein Digest")

    async def lauf():
        stack = await _Stack().start()
        erste = await stack.call(zwei)
        aid = erste.data["request_id"]
        require_equal(await stack.decide(aid), "ok")
        ergebnis = await stack.call(einer, approval_id=aid)
        require_equal(ergebnis.reason, "approval_drift", str(ergebnis))
        require(stack.store.get("mein Sohn") is None, "eine andere Bindung wurde geschrieben")
        # Die freigegebene laeuft.
        ergebnis = await stack.call(zwei, approval_id=aid)
        require_equal(ergebnis.outcome, CapabilityOutcome.SUCCESS, str(ergebnis))
        require_equal(len(stack.store.get("mein Sohn")["handles"]), 2)
    asyncio.run(lauf())


def t_the_store_compares_and_swaps_on_content_and_version():
    """Die Schranke im Speicher selbst — Inhalt UND Fassung."""
    store = BindingStore(os.path.join(tempfile.mkdtemp(), "cas.sqlite3"))
    require_equal(store.snapshot("niemand"), ABSENT)
    erste = store.confirm("ich", "Mara Muster", [{"channel": "phone", "value": ALT}],
                          "test", expected=ABSENT)
    require_equal(erste["version"], 1)
    stand = store.snapshot("ich")
    # Ein Fremdschreiber dazwischen — gleicher Inhalt, aber eine neue Fassung.
    store.confirm("ich", "Mara Muster", [{"channel": "phone", "value": ALT}], "test")
    require_equal(store.get("ich")["version"], 2)
    try:
        store.confirm("ich", "Mara Muster", [{"channel": "phone", "value": NEU}],
                      "test", expected=stand)
        require(False, "der Speicher schrieb ueber eine fremde Fassung")
    except BindingChanged:
        pass
    require_equal(store.get("ich")["handles"][0]["value"], ALT,
                  "trotz BindingChanged wurde geschrieben")
    require_equal(store.get("ich")["version"], 2, "eine abgelehnte Schreibung zaehlte hoch")
    # Mit dem richtigen Stand geht es — genau einmal.
    zweite = store.confirm("ich", "Mara Muster", [{"channel": "phone", "value": NEU}],
                           "test", expected=store.snapshot("ich"))
    require_equal(zweite["version"], 3)
    require(fingerprint(zweite) != stand, "zwei verschiedene Staende, ein Fingerabdruck")


# =========================================================================
# G — Genau die bestaetigte Mutation, genau einmal
# =========================================================================
def t_an_approved_binding_is_written_exactly_once():
    async def lauf():
        stack = await _Stack().start()
        _seed(stack, ALT)
        erste = await stack.call(_args(NEU))
        aid = erste.data["request_id"]
        require_equal(await stack.decide(aid), "ok")

        ergebnis = await stack.call(_args(NEU), approval_id=aid)
        require_equal(ergebnis.outcome, CapabilityOutcome.SUCCESS, str(ergebnis))
        require_equal(ergebnis.data["changed"], True)
        jetzt = stack.store.get("mein Sohn")
        require_equal(jetzt["handles"], [{"channel": "phone", "value": NEU}])
        require_equal(jetzt["version"], 2, "die Fassung zaehlt nicht je Schreibung")
        require_equal(await stack.state(aid), S.CONSUMED, "die Freigabe ist nicht verbraucht")

        # Dieselbe Freigabe noch einmal, dieselben Argumente ohne Kennung, und
        # eine erfundene Kennung: nichts davon schreibt ein zweites Mal.
        for kennung in (aid, None, "ap-erfunden"):
            wieder = await stack.call(_args(NEU), approval_id=kennung)
            require(wieder.outcome is not CapabilityOutcome.SUCCESS
                    or wieder.data.get("changed") is False, str(wieder))
            require_equal(stack.store.get("mein Sohn"), jetzt, f"zweite Schreibung ({kennung})")
        require_equal(len(await stack.requests()), 1,
                      "eine erledigte Bindung erzeugte eine neue Freigabefrage")
    asyncio.run(lauf())


def t_the_approved_binding_is_exactly_what_the_owner_saw():
    """Freigegeben wird die Anzeige, ausgefuehrt die Faehigkeit — beides muss dasselbe sein."""
    async def lauf():
        stack = await _Stack().start()
        erste = await stack.call(_args(NEU, alias="  Mein   SOHN ", name="Alex Muster"))
        aid = erste.data["request_id"]
        text = (await stack.requests())[0]["task"]
        require_equal(await stack.decide(aid), "ok")
        ergebnis = await stack.call(_args(NEU, alias="  Mein   SOHN ", name="Alex Muster"),
                                    approval_id=aid)
        require_equal(ergebnis.outcome, CapabilityOutcome.SUCCESS, str(ergebnis))
        geschrieben = stack.store.get("mein sohn")
        require(geschrieben["display_name"] in text, "geschriebener Name stand nicht im Text")
        for weg in geschrieben["handles"]:
            require(weg["value"] in text, "geschriebener Kontaktweg stand nicht im Text")
        require_equal(geschrieben["version"], 1)
    asyncio.run(lauf())


# =========================================================================
# H — Kein Modell und kein Agent erzeugt Autoritaet fuer eine Bindung
# =========================================================================
def t_the_model_cannot_manufacture_authority_for_a_binding():
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack)
        # 1. Kein Feld im Schema traegt Autoritaet — und Unbekanntes faellt am Tor.
        felder = set(_SCHEMAS[NAME]["parameters"]["properties"])
        require_equal(felder, {"alias", "display_name", "handles", "source"})
        for schmuggel in ({"approved": True}, {"approval_request_id": "ap-x"},
                          {"expected": ABSENT}, {"face_id": "ok"}):
            ergebnis = await stack.call(dict(_args(NEU), **schmuggel))
            require_equal(ergebnis.outcome, CapabilityOutcome.INVALID_INPUT, str(ergebnis))
            require(ergebnis.reason.startswith("unknown_argument:"), ergebnis.reason)
        # 2. Was das Modell selbst erzeugt hat, traegt keine Autoritaet.
        ergebnis = await stack.call(_args(NEU), trust=voice_trust(False))
        require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(ergebnis))
        require_equal(ergebnis.reason, "no_user_authority", ergebnis.reason)
        # 3. Eine ausgedachte Freigabekennung fuehrt nichts aus.
        ergebnis = await stack.call(_args(NEU), approval_id="ap-ausgedacht")
        require(ergebnis.outcome is not CapabilityOutcome.SUCCESS, str(ergebnis))
        require_equal(await stack.requests(), [],
                      "Schmuggel, Modelltext oder eine erfundene Kennung erzeugten eine Freigabe")
        # 4. Keine Anweisung, sondern eine Frage oder ein Zitat: dann entscheidet
        #    das Geraet — nie der Turn selbst. Face ID, keine Direktausfuehrung.
        ergebnis = await stack.call(_args(NEU), commanded=False)
        require_equal(ergebnis.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(ergebnis))
        require_equal(stack.store.get("mein Sohn"), vorher)
    asyncio.run(lauf())


def t_the_handler_writes_nothing_without_the_frozen_path():
    """Der Handler allein — ohne freigegebenen Vorgang, mit fremdem Digest — schreibt nicht."""
    stack = _Stack()
    vorher = _seed(stack)
    # Ohne freigegebenen Vorgang.
    try:
        asyncio.run(stack.cap.confirm_binding(_args(NEU)))
        require(False, "ohne freigegebenen Vorgang geschrieben")
    except SafeExecutionFailure as exc:
        require_equal(str(exc), "binding_without_approval")
    # Mit Vorgang, aber ohne Digest — und mit dem Digest einer ANDEREN Bindung.
    for digest in ("", _digest(stack.cap.describe_confirm_binding(_args(DRITTE)))):
        with _bound(digest):
            try:
                asyncio.run(stack.cap.confirm_binding(_args(NEU)))
                require(False, f"mit fremdem Digest {digest[:8]!r} geschrieben")
            except SafeExecutionFailure as exc:
                require(str(exc) in ("binding_without_approval",
                                     "binding_changed_after_approval"), str(exc))
    # Und mit dem Digest einer anderen Herkunft: der Text traegt sie mit.
    with _bound(_digest(stack.cap.describe_confirm_binding(_args(NEU)), IPHONE)):
        try:
            asyncio.run(stack.cap.confirm_binding(_args(NEU)))
            require(False, "eine Freigabe aus anderer Herkunft wurde eingeloest")
        except SafeExecutionFailure as exc:
            require_equal(str(exc), "binding_changed_after_approval")
    require_equal(stack.store.get("mein Sohn"), vorher)
    # Kein Verfeinerer koennte die Geburtsregel unterlaufen (DEBT-0207).
    router = CapabilityRouter()
    register(router, stack.cap)
    require(router._classifiers.get(NAME) is None,
            "ein Klassifizierer an der Bindung koennte die Geburtsregel unterlaufen")


# =========================================================================
# No-op — dieselbe Bindung noch einmal: nichts zu bestaetigen, nichts geschrieben
# =========================================================================
def t_confirming_the_identical_binding_is_a_no_op_without_face_id():
    async def lauf():
        stack = await _Stack().start()
        vorher = _seed(stack, ALT)
        for origin in (IPHONE, AP.OriginClass.ROOM_VOICE, AP.OriginClass.LOCAL_OWNER):
            # Andere Reihenfolge der Kontaktwege, andere Quelle: dieselbe Bindung.
            gleich = {"alias": "MEIN sohn", "display_name": "Alex Muster",
                      "handles": [{"channel": "PHONE", "value": f" {ALT} "}],
                      "source": "model_repeat"}
            ergebnis = await stack.call(gleich, origin=origin)
            require_equal(ergebnis.reason, "binding_unchanged", f"{origin.value}: {ergebnis}")
            require_equal(ergebnis.data, {"confirmed": True, "changed": False,
                                          "binding": vorher})
        require_equal(await stack.requests(), [], "ein No-op erzeugte eine Freigabefrage")
        require_equal(stack.store.get("mein Sohn"), vorher, "ein No-op hat geschrieben")
        # Sobald sich ein autoritaetsrelevanter Wert aendert, ist es keiner mehr.
        for anders in (_args(NEU), _args(ALT, name="Alexander Muster"),
                       dict(_args(ALT), handles=[{"channel": "phone", "value": ALT},
                                                 {"channel": "gmail",
                                                  "value": "alex@example.test"}])):
            ergebnis = await stack.call(anders)
            require_equal(ergebnis.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(ergebnis))
        require_equal(stack.store.get("mein Sohn"), vorher)
    asyncio.run(lauf())


def t_a_schedule_learns_nothing_from_a_no_op():
    """Ein No-op ist kein Orakel: aus dem Hintergrund und aus fremdem Inhalt bleibt es DENY."""
    async def lauf():
        stack = await _Stack().start()
        _seed(stack, ALT)
        vertrauen = TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True)
        for origin in (AP.OriginClass.BACKGROUND_AUTOMATION,
                       AP.OriginClass.EXTERNAL_UNTRUSTED):
            ergebnis = await stack.call(
                _args(ALT), origin=origin, trust=vertrauen,
                provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in _args()})
            require_equal(ergebnis.reason, "policy_denied", f"{origin.value}: {ergebnis}")
            require(ergebnis.data is None or "binding" not in (ergebnis.data or {}),
                    "ein DENY verriet den Stand der Bindung")
    asyncio.run(lauf())


# =========================================================================
# Gegenprobe — die Geburtsregel ist die tragende Schranke
# =========================================================================
def t_without_the_birth_rule_the_iphone_row_would_rebind_directly():
    """Die Mutation, die DEBT-0208 war: Name aus der Geburtsregel, CRITICAL in der Registry.

    Unter dieser Mutation sagt die Politik vom iPhone aus EXECUTE_DIRECTLY —
    genau der gemessene Befund. Die Zusicherungen oben wuerden es sehen. Und
    die zweite Schranke im Handler schreibt selbst dann nicht.
    """
    echt_geburt, echt_registry = AP.VERY_CRITICAL_BY_BIRTH, dict(AP.ACTION_CLASS)
    try:
        AP.VERY_CRITICAL_BY_BIRTH = frozenset(echt_geburt - {NAME})
        AP.ACTION_CLASS[NAME] = AP.ActionClass.CRITICAL
        klasse = AP.base_class(NAME, read_only=False)
        require_equal(AP.decide(IPHONE, klasse, capability=NAME).decision,
                      AP.Decision.EXECUTE_DIRECTLY,
                      "die Gegenprobe reproduziert DEBT-0208 nicht — die Zusicherung "
                      "oben misst dann nichts")

        async def lauf():
            stack = await _Stack().start()
            vorher = _seed(stack, ALT)
            ergebnis = await stack.call(_args(NEU))
            require(ergebnis.outcome is not CapabilityOutcome.APPROVAL_REQUIRED,
                    "unter der Mutation wurde trotzdem gefragt — die Gegenprobe greift nicht")
            require_equal(await stack.requests(), [])
            require_equal(stack.store.get("mein Sohn"), vorher,
                          "ohne Geburtsregel schrieb der Handler ohne Freigabe")
        asyncio.run(lauf())
    finally:
        AP.VERY_CRITICAL_BY_BIRTH = echt_geburt
        AP.ACTION_CLASS.clear()
        AP.ACTION_CLASS.update(echt_registry)
    require_equal(AP.decide(IPHONE, AP.base_class(NAME, read_only=False),
                            capability=NAME).decision, AP.Decision.REQUIRE_FACE_ID)


# =========================================================================
# Bestand, Modellkontext, Testdaten
# =========================================================================
def t_an_existing_store_without_a_version_column_is_upgraded_in_place():
    """Der produktive Bestand stammt von vor diesem Milestone — und bleibt lesbar."""
    pfad = os.path.join(tempfile.mkdtemp(), "alt.sqlite3")
    con = sqlite3.connect(pfad)
    con.execute("CREATE TABLE bindings (alias_norm TEXT PRIMARY KEY, alias TEXT NOT NULL, "
                "display_name TEXT NOT NULL, handles TEXT NOT NULL, confirmed_at REAL "
                "NOT NULL, source TEXT NOT NULL)")
    con.execute("INSERT INTO bindings VALUES (?, ?, ?, ?, ?, ?)",
                ("ich", "ich", "Mara Muster",
                 '[{"channel":"phone","value":"%s"}]' % ALT, 1.0, "owner_confirmed_locally"))
    con.commit(); con.close()

    store = BindingStore(pfad)
    alt = store.get("ich")
    require_equal(alt["version"], 0, "eine Bestandszeile bekam eine erfundene Fassung")
    require_equal(alt["handles"], [{"channel": "phone", "value": ALT}])
    neu = store.confirm("ich", "Mara Muster", [{"channel": "phone", "value": NEU}], "test",
                        expected=store.snapshot("ich"))
    require_equal(neu["version"], 1)
    require_equal(store.get("ich")["source"], "test")
    # Zweimal oeffnen, und das Rennen zweier Erstoeffner: der zweite sieht die
    # Spalte beim ersten Blick noch nicht, sein ALTER scheitert an der
    # inzwischen vorhandenen Spalte — und das darf kein Fehler sein.
    require_equal(BindingStore(pfad).get("ich")["version"], 1)
    blicke = []

    class _Zweiter(BindingStore):
        def _spalten(self):
            blicke.append(1)
            return set() if len(blicke) == 1 else super()._spalten()

    zweiter = _Zweiter(pfad)
    require_equal(len(blicke), 2, "das Rennen wurde nicht nachgestellt")
    require_equal(zweiter.get("ich")["version"], 1, "die Nachziehung hat Daten verloren")

    # Fehlt die Spalte auch nach dem gescheiterten ALTER, ist das ein Fehler.
    class _Kaputt(BindingStore):
        def _spalten(self):
            return set()

    try:
        _Kaputt(pfad)
        require(False, "eine fehlende Spalte nach dem ALTER wurde verschluckt")
    except sqlite3.OperationalError:
        pass


def t_no_test_can_open_the_production_contact_store():
    """Der Standardpfad zeigt unter Tests nie auf `~/.solvio`.

    Gemessen am 2026-09-05: eine Zusicherung oeffnete den produktiven
    Kontaktspeicher ueber den Standardpfad, und die additive Nachziehung
    dieses Milestones lief damit auf dem Bestand des laufenden Cores statt in
    einem Testverzeichnis. Seither setzt `tests/_guard.py` den Pfad um, bevor
    irgendeine Suite einen Speicher anfasst.
    """
    produktiv = os.path.expanduser("~/.solvio")
    require(os.environ.get("SOLVIO_CONTACTS_DB", ""),
            "der Guard setzt den Kontaktspeicher-Pfad nicht um")
    store = BindingStore()
    require(not os.path.abspath(store.path).startswith(produktiv + os.sep),
            f"ein Test kann den produktiven Kontaktspeicher oeffnen: {store.path}")
    require(store.get("ich") is None and store.get("mich") is None,
            "der Teststandardpfad enthaelt Bindungen — das ist nicht der Testspeicher")


def t_the_model_gets_a_request_id_back_and_nothing_else():
    """Vor der Freigabe erreicht kein Kontaktweg das Modell — nur eine Kennung."""
    async def lauf():
        stack = await _Stack().start()
        _seed(stack, ALT)
        ergebnis = await stack.call(_args(NEU))
        require_equal(set(ergebnis.data), {"request_id"})
        require(ALT not in repr(ergebnis) and NEU not in repr(ergebnis),
                "ein Kontaktweg stand im Ergebnisumschlag")
    asyncio.run(lauf())


def t_the_test_data_is_unmistakably_synthetic():
    """Rufnummern nur aus dem Fiktionsbereich, Adressen nur unter example.test."""
    with open(__file__, encoding="utf-8") as fh:
        quelle = fh.read()
    for nummer in set(re.findall(r"\+\d{10,}", quelle)):
        require(re.fullmatch(r"\+447700900\d{3}", nummer),
                f"nicht erkennbar erfundene Rufnummer im Test: {nummer}")
    for adresse in set(re.findall(r"[\w.+-]+@[\w.-]+", quelle)):
        require(adresse.endswith("@example.test"),
                f"nicht erkennbar erfundene Adresse im Test: {adresse}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
