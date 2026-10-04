"""DEBT-0104 — was der Mensch freigibt, ist auch das, was hinausgeht.

Der Befund kam aus der Architekturpruefung zu Approval Policy V2 und war kein
Randfall: `gmail_send_draft` prueft vor dem Versand den gespeicherten Entwurf,
aber bis hierher nur auf Empfaenger und Betreff. Der TEXT wurde nicht
verglichen — und gesendet wird der Entwurf ueber seine Kennung, also das, was
bei Gmail liegt. Haette ihn zwischen Freigabe und Versand irgendetwas geaendert
(ein zweiter Client, eine Synchronisierung, ein Skript), waere ein anderer Text
hinausgegangen als der, den der Mensch Wort fuer Wort auf dem Display gelesen
hat.

Das ist gerade jetzt entscheidend: seit Approval Policy V2 darf ein bewusst am
iPhone gesprochener Versand OHNE zweite Face-ID-Runde laufen. Eine Bindung, die
den Inhalt auslaesst, waere damit vom Komfortgewinn zur Luecke geworden.

Geprueft wird deshalb auf ROUTER-Ebene und nicht nur am Handler: der Weg, den
eine echte Anfrage nimmt, ist der Weg, der halten muss.

Seit ADR-0041 (26.09.2026) fragt jeder Versand Face ID, der Freigabetext ist der
tatsaechliche Entwurf (Beschreiber), und hinaus gehen genau die freigegebenen Bytes
(„aktualisieren und senden"). Die Proben hier laufen deshalb am echten Freigabeweg.
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.envelope import CapabilityOutcome  # noqa: E402
from solvio.capabilities.gmail import (  # noqa: E402
    SPECS, GmailCapabilities, _normalized, register as register_gmail,
)
from solvio.capabilities.invocation import (  # noqa: E402
    CapabilityInvocationGate, voice_trust,
)
from solvio.capabilities.policy import OriginClass  # noqa: E402
from solvio.capabilities.router import CapabilityRouter  # noqa: E402


class _Stack:
    """Der produktive Weg seit ADR-0041: echter Router, echte iPhone-Freigabekette (nur das
    signierende Geraet ist synthetisch), echter Gmail-Client ueber einer Gmail-REST-
    Nachbildung (Entwuerfe als MIME, Anhaenge mit Kennung, „aktualisieren und senden")."""

    async def open(self):
        import tempfile
        from mail_send_harness import GmailApi, approval_chain
        from solvio.capabilities.approval_gateway import CapabilityApprovals
        self.cp, self.co, self.device, self.H, self.storage = await approval_chain(
            tempfile.mkdtemp(prefix="solvio-exact-"))
        self.router = CapabilityRouter(
            mobile=CapabilityApprovals(self.co, owner_principal="local-owner"),
            policy_mode="enforce")
        self.api = GmailApi()
        register_gmail(self.router, GmailCapabilities(self.api))
        self.gate = CapabilityInvocationGate()
        self.counter = [0]
        return self

    async def draft(self, *, to="max@example.com", subject="Hallo", body="Text.", files=()):
        created = await self.api.create_draft(to=to, subject=subject, body=body,
                                              attachments=tuple(files))
        return created["id"]

    def change(self, draft_id, *, to="max@example.com", subject="Hallo", body="Text.", files=()):
        from mail_send_harness import mime_with_attachment
        self.api.replace_draft(draft_id, mime_with_attachment(
            sender="owner@example.test", to=to, subject=subject, body=body, files=list(files)))

    async def send(self, args, *, approval_id=None):
        self.gate.begin_turn(session_id="s", turn_id="t", principal="pi",
                             origin=OriginClass.TRUSTED_INTERACTIVE_APP,
                             trust=voice_trust(True), user_text=_SAID)
        context = self.gate.context()
        return await self.router.execute("gmail_send_draft", args, trust=context.trust,
                                         provenance=self.gate.provenance_for(args),
                                         principal=context.principal, origin=context.origin,
                                         commanded=context.commanded,
                                         approval_request_id=approval_id)

    async def approve(self, request_id):
        from mail_send_harness import sign
        await sign(self.cp, self.co, self.device, self.H, request_id, self.counter)


_SAID = "Schick die Mail an max@example.com ab."
_PDF = b"%PDF-1.4 gehaltsabrechnung"


# =====================================================================
# Der Inhalt ist gebunden
# =====================================================================

async def t_ein_unveraenderter_entwurf_geht_hinaus():
    """Die Gegenprobe zuerst. Ohne sie waere die Regel nur streng."""
    from mail_send_harness import sent_message
    st = await _Stack().open()
    try:
        draft_id = await st.draft(body="Wir sehen uns um acht.")
        args = {"draft_id": draft_id, "to": "max@example.com", "subject": "Hallo",
                "body": "Wir sehen uns um acht."}
        first = await st.send(args)
        require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(first))
        await st.approve(first.data["request_id"])
        result = await st.send(args, approval_id=first.data["request_id"])
        require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
        require_equal(len(st.api.sent), 1, "die Mail ging nicht raus")
        require_equal(sent_message(st.api.sent[0]).get_content().strip(), "Wir sehen uns um acht.")
    finally:
        await st.storage.close()


async def t_ein_geaenderter_text_geht_nicht_hinaus():
    """DER DEFEKT von DEBT-0104. Genannt war ein Satz, im Entwurf steht ein anderer — endet
    vor der Frage; und nach der Freigabe geaendert, driftet die Freigabe."""
    st = await _Stack().open()
    try:
        draft_id = await st.draft(body="Wir sehen uns um acht.")
        st.change(draft_id, body="Bitte ueberweise 500 Euro auf DE00.")
        result = await st.send({"draft_id": draft_id, "to": "max@example.com",
                                "subject": "Hallo", "body": "Wir sehen uns um acht."})
        require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
        require_equal(result.reason, "draft_body_mismatch", str(result))
        require_equal(await st.storage.list_pending(), [], "trotzdem gefragt")

        draft_id = await st.draft(body="Wir sehen uns um acht.")
        first = await st.send({"draft_id": draft_id})
        await st.approve(first.data["request_id"])
        st.change(draft_id, body="Bitte ueberweise 500 Euro auf DE00.")
        result = await st.send({"draft_id": draft_id}, approval_id=first.data["request_id"])
        require(result.outcome is not CapabilityOutcome.SUCCESS, str(result))
        require(not st.api.sent, "ein fremder Text ist hinausgegangen")
    finally:
        await st.storage.close()


async def t_auch_eine_kleine_aenderung_faellt_auf():
    """Ein einziges Wort genuegt — sonst waere die Bindung nur Dekoration."""
    st = await _Stack().open()
    try:
        draft_id = await st.draft(body="Ich komme um acht.")
        first = await st.send({"draft_id": draft_id})
        await st.approve(first.data["request_id"])
        st.change(draft_id, body="Ich komme nicht um acht.")
        result = await st.send({"draft_id": draft_id}, approval_id=first.data["request_id"])
        require(result.outcome is not CapabilityOutcome.SUCCESS, str(result))
        require(not st.api.sent, "eine Verneinung wurde mitgesendet")
    finally:
        await st.storage.close()


async def t_ein_angehaengtes_dokument_stoppt_den_versand():
    """Ein Anhang geht nur mit, wenn er im Freigabetext stand (ADR-0041). Kommt er nach der
    Freigabe dazu, driftet sie — und nichts geht hinaus."""
    st = await _Stack().open()
    try:
        draft_id = await st.draft(body="Anbei.")
        first = await st.send({"draft_id": draft_id})
        shown = (await st.storage.get_request(first.data["request_id"]))["task"]
        require("gehaltsabrechnung" not in shown and "Anhänge" not in shown, shown)
        await st.approve(first.data["request_id"])
        st.change(draft_id, body="Anbei.", files=[("gehaltsabrechnung.pdf", "application/pdf", _PDF)])
        result = await st.send({"draft_id": draft_id}, approval_id=first.data["request_id"])
        require(result.outcome is not CapabilityOutcome.SUCCESS, str(result))
        require(not st.api.sent, "ein unfreigegebener Anhang ging raus")
    finally:
        await st.storage.close()


async def t_empfaenger_und_betreff_bleiben_gebunden():
    """Nennt das Modell Empfaenger oder Betreff, muessen sie zum Entwurf passen — vor der Frage."""
    for field, value, reason in (("to", "eve@example.com", "draft_recipient_mismatch"),
                                 ("subject", "Rechnung", "draft_subject_mismatch")):
        st = await _Stack().open()
        try:
            draft_id = await st.draft()
            st.change(draft_id, **{field: value})
            result = await st.send({"draft_id": draft_id, "to": "max@example.com",
                                    "subject": "Hallo", "body": "Text."})
            require_equal(result.reason, reason, f"{field}: {result}")
            require(not st.api.sent, f"{field} war nicht gebunden")
        finally:
            await st.storage.close()


# =====================================================================
# Nachsichtig gegenueber Darstellung, streng gegenueber Inhalt
# =====================================================================

def t_zeilenenden_sind_kein_inhaltlicher_unterschied():
    """Sonst scheiterte jeder zweite Versand an Transportkosmetik.

    Eine Bindung, die an `\\r\\n` zerbricht, wuerde in der Praxis abgeschaltet —
    und eine abgeschaltete Bindung schuetzt nichts.
    """
    require_equal(_normalized("Hallo\r\nWelt  \n\n"), _normalized("Hallo\nWelt"),
                  "Zeilenenden wurden als Inhalt gewertet")


def t_ein_zusaetzliches_wort_ist_ein_inhaltlicher_unterschied():
    require(_normalized("Bitte kommen") != _normalized("Bitte nicht kommen"),
            "eine Verneinung galt als derselbe Text")


# =====================================================================
# Die Bindung haengt am Weg, nicht am Handler
# =====================================================================

def t_der_freigabetext_enthaelt_den_ganzen_inhalt():
    """Was gebunden wird, muss der Mensch auch sehen koennen.

    Der Digest laeuft ueber den angezeigten Text. Stuende der Inhalt nicht
    darin, waere die Bindung ein Versprechen ueber etwas Unsichtbares.
    """
    from solvio.capabilities.approval_gateway import render_action
    text = render_action(SPECS["gmail_send_draft"],
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Wir sehen uns um acht."},
                         "iPhone-App")
    for part in ("max@example.com", "Hallo", "Wir sehen uns um acht.",
                 "Angefragt über: iPhone-App"):
        require(part in text, f"{part!r} fehlte im Freigabetext:\n{text}")


async def t_der_versandweg_prueft_den_inhalt_wirklich():
    """Gegen die naheliegendste Regression: jemand entfernt die Pruefung im Handler.

    Am Verhalten, nicht am Quelltext: ausserhalb einer freigegebenen Ausfuehrung sendet der
    Handler nie, und innerhalb nur, wenn der Entwurf noch genau die freigegebene
    Beschreibung ist."""
    from solvio.capabilities.router import _WithArguments
    from solvio.security.mobile_approval.execution import SafeExecutionFailure
    st = await _Stack().open()
    try:
        caps = GmailCapabilities(st.api)
        draft_id = await st.draft(body="Wir sehen uns um acht.")
        approved = await caps.describe_send({"draft_id": draft_id})
        for label, call in (
                ("ohne Freigabe", caps.send_draft({"draft_id": draft_id})),
                ("fremde Freigabe", _WithArguments(caps.send_draft, {"draft_id": draft_id})(
                    {**approved, "draft_id": "anderer"}))):
            try:
                await call
            except SafeExecutionFailure as exc:
                require_equal(str(exc), "send_not_approved", label)
            else:
                raise AssertionError(label + ": gesendet")
        st.change(draft_id, body="Wir sehen uns um neun.")
        try:
            await _WithArguments(caps.send_draft, {"draft_id": draft_id})(approved)
        except SafeExecutionFailure as exc:
            require_equal(str(exc), "draft_changed")
        else:
            raise AssertionError("ein geaenderter Entwurf ging hinaus")
        require(not st.api.sent)
    finally:
        await st.storage.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
