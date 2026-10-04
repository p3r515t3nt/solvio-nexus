"""Stage S2 (ADR-0041): a mail leaves the house only after the owner saw it and said yes.

Measured on 26.09.2026: from the iPhone app a spoken send ran WITHOUT Face ID
(`TRUSTED_INTERACTIVE_APP x CRITICAL = EXECUTE_DIRECTLY`), a recipient the model
copied from a read mail counted only as MODEL_DERIVED, and forwarding a receipt
with its PDF was impossible (`gmail_send_draft` refused every attachment). The
owner decided: every outbound mail, always Face ID, the screen shows recipient,
subject, text and attachments.

Everything here runs the production path: the real router, the real mobile
approval chain (only the signing device is synthetic), the real Gmail client
over an in-memory Gmail REST surface (`mail_send_harness`).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from mail_send_harness import (  # noqa: E402
    OWN, GmailApi, approval_chain, mime_with_attachment, sent_message, sign)

SANDBOX = tempfile.mkdtemp(prefix="solvio-mail-s2-")
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 40 + b"\n%%EOF\n"
WEB = "gregor-brawanski@web.test"
SAID_FORWARD = "Leite die Rechnung von ElevenLabs an gregor minus brawanski at web punkt test weiter."
SAID_SEND = "Ja, schick sie ab."
RECEIPT_ID = "<receipt-1@elevenlabs.test>"


def _receipt(api: GmailApi, *, body: str = "Thanks for your payment.\nAmount: 22.00 USD",
             files=None, message_id: str = "m-receipt") -> None:
    api.inbox[message_id] = mime_with_attachment(
        sender="ElevenLabs <billing@elevenlabs.test>", to=OWN, message_id=RECEIPT_ID,
        subject="Your receipt from ElevenLabs Inc.", body=body,
        files=[("receipt.pdf", "application/pdf", PDF)] if files is None else files)



def _raw_draft(*, to: str, body: str, subject: str = "Fwd: Your receipt from ElevenLabs Inc.",
               html: str = "", extra_parts=(), headers=None, files=None) -> bytes:
    """A draft as another client could leave it at Gmail."""
    from email.message import EmailMessage as Mime
    message = Mime()
    message["From"] = OWN
    message["To"] = to
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    for name, mime_type, data in ([("receipt.pdf", "application/pdf", PDF)] if files is None else files):
        maintype, subtype = mime_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    for mime_type, data, _name in extra_parts:
        maintype, subtype = mime_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype)
        part = list(message.iter_attachments())[-1]
        del part["Content-Disposition"]
    return message.as_bytes()

def _gate(said: str, origin=None):
    from solvio.capabilities import policy as P
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.contracts.trust import TrustContext, TrustLevel
    gate = CapabilityInvocationGate()
    gate.begin_turn(session_id="s-mail", turn_id="s-mail-t1", principal="iphone-dev-test",
                    trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                       user_authorized=True, note="Sprachturn"),
                    user_text=said,
                    origin=origin or P.OriginClass.TRUSTED_INTERACTIVE_APP)
    return gate


async def _stack(**kw):
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.gmail import GmailCapabilities, register
    from solvio.capabilities.router import CapabilityRouter
    cp, co, device, H, storage = await approval_chain(SANDBOX)
    router = CapabilityRouter(mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                              policy_mode="enforce")
    api = GmailApi(**kw)
    _receipt(api)
    caps = GmailCapabilities(api)
    register(router, caps)
    return SimpleNamespace(cp=cp, co=co, device=device, H=H, storage=storage,
                           router=router, api=api, caps=caps, counter=[0])


async def _exec(s, name, args, said, *, approval_id=None, origin=None):
    gate = _gate(said, origin)
    ctx = gate.context()
    return await s.router.execute(name, args, trust=ctx.trust,
                                  provenance=gate.provenance_for(args),
                                  principal=ctx.principal, origin=ctx.origin,
                                  commanded=ctx.commanded, approval_request_id=approval_id)


async def _forward(s, **extra):
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    args = {"forward_message": "m-receipt", "to": WEB, "body": "", **extra}
    result = await _exec(s, "gmail_create_draft", args, SAID_FORWARD)
    require_equal(result.outcome, OUT.SUCCESS, str(result))
    return result.data


# =====================================================================
# The rule
# =====================================================================

def t_every_outbound_mail_needs_face_id_from_every_origin_and_only_mail_is_tightened():
    from solvio.capabilities import policy as P
    for capability in ("gmail_send_draft", "communication_send"):
        require_equal(P.base_class(capability, read_only=False), P.ActionClass.CRITICAL)
        for origin in (P.OriginClass.TRUSTED_INTERACTIVE_APP, P.OriginClass.ROOM_VOICE,
                       P.OriginClass.LOCAL_OWNER, P.OriginClass.TRUSTED_DASHBOARD,
                       P.OriginClass.BACKGROUND_AUTOMATION, P.OriginClass.UNSPECIFIED):
            outcome = P.decide(origin, P.ActionClass.CRITICAL, capability=capability)
            require_equal(outcome.decision, P.Decision.REQUIRE_FACE_ID,
                          f"{capability} from {origin.value} went out unseen")
        app = P.decide(P.OriginClass.TRUSTED_INTERACTIVE_APP, P.ActionClass.CRITICAL,
                       capability=capability)
        require_equal(app.reason_code, "outbound_message_face_id")
        require_equal(P.decide(P.OriginClass.EXTERNAL_UNTRUSTED, P.ActionClass.CRITICAL,
                               capability=capability).decision, P.Decision.DENY,
                      "the tightening must never loosen a DENY")
    # Narrow: the V2 comfort for everything else from the phone stays as decided.
    require_equal(P.decide(P.OriginClass.TRUSTED_INTERACTIVE_APP, P.ActionClass.CRITICAL,
                           capability="calendar_delete_event").decision,
                  P.Decision.EXECUTE_DIRECTLY)
    require_equal(P.decide(P.OriginClass.TRUSTED_INTERACTIVE_APP, P.ActionClass.NORMAL_WRITE,
                           capability="gmail_create_draft").decision,
                  P.Decision.EXECUTE_DIRECTLY, "a draft tells nobody anything")


# =====================================================================
# Forwarding the receipt — the owner's real request of 26.09.2026
# =====================================================================

async def t_a_forwarded_receipt_leaves_only_after_face_id_with_its_pdf_byte_for_byte():
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    from solvio.capabilities.gmail import attachment_line
    s = await _stack()
    try:
        draft = await _forward(s)
        require_equal(draft["attachments"], [attachment_line("receipt.pdf", PDF)])
        require(draft["sent"] is False and s.api.sent == [], "a draft went out")
        require("Weitergeleitete Nachricht" in draft["body"] and "22.00 USD" in draft["body"],
                draft["body"])

        first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
        require_equal(first.outcome, OUT.APPROVAL_REQUIRED,
                      f"from the phone the mail went out without Face ID: {first}")
        require_equal(s.api.sent, [], "sent before Face ID")
        request_id = first.data["request_id"]
        shown = (await s.storage.get_request(request_id))["task"]
        for part in ("E-Mail senden", WEB, "Fwd: Your receipt from ElevenLabs Inc.",
                     "Anhänge", "receipt.pdf", f"{len(PDF)} Bytes",
                     attachment_line("receipt.pdf", PDF).rsplit(" ", 1)[1],
                     "Weitergeleitete Nachricht", "22.00 USD"):
            require(part in shown, f"the Face ID text does not show {part!r}:\n{shown}")

        await sign(s.cp, s.co, s.device, s.H, request_id, s.counter)
        second = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                             approval_id=request_id)
        require_equal(second.outcome, OUT.SUCCESS, str(second))
        require(second.data["sent"] is True and second.data["message_id"], second.data)
        require_equal(len(s.api.sent), 1)
        mail = sent_message(s.api.sent[0])
        require_equal(mail["To"], WEB)
        files = [(p.get_filename(), p.get_payload(decode=True)) for p in mail.iter_attachments()]
        require_equal(files, [("receipt.pdf", PDF)], "the PDF did not leave byte for byte")

        again = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                            approval_id=request_id)
        require(again.outcome is not OUT.SUCCESS, "a used approval sent a second time")
        require_equal(len(s.api.sent), 1)
        # The approval belonged to THAT execution only (review S2, finding 6).
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        try:
            await s.caps.send_draft({"draft_id": draft["draft_id"]})
        except SafeExecutionFailure as exc:
            require_equal(str(exc), "send_not_approved")
        else:
            raise AssertionError("the approval outlived its execution")
    finally:
        await s.storage.close()


async def t_a_draft_changed_after_face_id_is_never_sent():
    """What Face ID confirmed is bound: another PDF or another recipient drifts."""
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    for change in ("attachment", "recipient"):
        s = await _stack()
        try:
            draft = await _forward(s)
            first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
            request_id = first.data["request_id"]
            await sign(s.cp, s.co, s.device, s.H, request_id, s.counter)
            other = mime_with_attachment(
                sender=OWN, to=("eve@example.test" if change == "recipient" else WEB),
                subject=draft["subject"], body=draft["body"],
                files=[("receipt.pdf", "application/pdf",
                        PDF[:-1] + b"X" if change == "attachment" else PDF)])
            s.api.replace_draft(draft["draft_id"], other)
            result = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]},
                                 SAID_SEND, approval_id=request_id)
            require(result.outcome is not OUT.SUCCESS, f"{change}: {result}")
            require_equal(s.api.sent, [], f"a changed draft ({change}) went out")
        finally:
            await s.storage.close()


async def t_what_leaves_is_exactly_the_approved_bytes_whatever_gmail_holds():
    """Review S2 (Befunde 1, 3, 6): gesendet wurde der Entwurf per Kennung — was bei Gmail
    lag, auch Unsichtbares. Jetzt wird die Mail aus der freigegebenen Beschreibung gebaut:
    aendert ein zweiter Client den Entwurf noch im Moment des Sendens, geht trotzdem nur
    das Freigegebene hinaus."""
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    s = await _stack()
    try:
        draft = await _forward(s)
        first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
        request_id = first.data["request_id"]
        await sign(s.cp, s.co, s.device, s.H, request_id, s.counter)

        def second_client(method, path):
            if method == "POST" and path == "/drafts/send":
                s.api.replace_draft(draft["draft_id"], _raw_draft(
                    to=WEB + ", eve@example.test", body=draft["body"] + "\nPS: Kontodaten",
                    extra_parts=[("application/pdf", b"secret", None)]))

        s.api.before_response = second_client
        result = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                             approval_id=request_id)
        require_equal(result.outcome, OUT.SUCCESS, str(result))
        mail = sent_message(s.api.sent[0])
        require_equal(mail["To"], WEB)
        require_equal(mail["Cc"], None)
        require_equal(mail.get_body(preferencelist=("plain",)).get_content().strip(), draft["body"])
        files = [(p.get_filename(), p.get_payload(decode=True)) for p in mail.iter_attachments()]
        require_equal(files, [("receipt.pdf", PDF)], "something else left with the mail")
    finally:
        await s.storage.close()


async def t_the_handler_sends_only_under_its_own_approval():
    """Nur innerhalb einer freigegebenen Ausfuehrung, nur fuer genau diesen Entwurf, nur
    wenn er noch der freigegebene ist — und eine gleichzeitige Beschreibung (Review S2,
    Befund 6) kann einen geaenderten Entwurf nicht unter eine alte Freigabe schieben."""
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    from solvio.capabilities.router import _WithArguments
    from solvio.security.mobile_approval.execution import SafeExecutionFailure
    s = await _stack()
    try:
        draft = await _forward(s)
        draft_id = draft["draft_id"]
        approved = await s.caps.describe_send({"draft_id": draft_id})
        for label, call, reason in (
                ("no approval", s.caps.send_draft({"draft_id": draft_id}), "send_not_approved"),
                ("other draft", _WithArguments(s.caps.send_draft, {"draft_id": draft_id})(
                    {**approved, "draft_id": "r-other"}), "send_not_approved")):
            try:
                await call
            except SafeExecutionFailure as exc:
                require_equal(str(exc), reason, label)
            else:
                raise AssertionError(label + ": sent")

        first = await _exec(s, "gmail_send_draft", {"draft_id": draft_id}, SAID_SEND)
        request_id = first.data["request_id"]
        await sign(s.cp, s.co, s.device, s.H, request_id, s.counter)
        s.api.replace_draft(draft_id, _raw_draft(to=WEB, body=draft["body"] + "\nPS: Kontodaten"))
        concurrent = await _exec(s, "gmail_send_draft", {"draft_id": draft_id}, SAID_SEND)
        require_equal(concurrent.outcome, OUT.APPROVAL_REQUIRED, str(concurrent))
        result = await _exec(s, "gmail_send_draft", {"draft_id": draft_id}, SAID_SEND,
                             approval_id=request_id)
        require(result.outcome is not OUT.SUCCESS, str(result))
        require_equal(s.api.sent, [], "a changed draft left under an old approval")
    finally:
        await s.storage.close()


async def t_hidden_or_extra_content_is_refused_before_anyone_is_asked():
    """Review S2 (Befunde 1, 3): Unsichtbares, ein HTML-Teil, ein unbenannter Datenteil,
    ein weiterer Empfaenger (auch mit Nicht-ASCII-Domain oder Resent-To) — nichts davon
    koennte der Bildschirm zeigen, also wird gar nicht erst gefragt."""
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    variants = {
        "zero-width body": dict(body="Hallo, anbei die Info.\u200b\u200b\u202e"),
        "blank-looking letters": dict(body="Danke fuer die Info!" + "\u3164\u2800" * 40),
        "bidi subject": dict(subject="Rechnung \u202egpj.exe"),
        "html alternative": dict(html="<p>Ganz andere Worte</p>"),
        "unnamed part": dict(extra_parts=[("application/pdf", b"secret", None)]),
        "idn recipient": dict(to=WEB + ", evil@\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444"),
        "resent-to": dict(headers={"Resent-To": "eve@example.test"}),
        # Review S2, round 3: a header that would print a fake line on the screen.
        "in-reply-to with newline": dict(headers={
            "In-Reply-To": "=?utf-8?q?<a@b.test>=0ABcc:_evil@x.test?="}),
        "in-reply-to free text": dict(headers={"In-Reply-To": '<a@b.test>", "An": "boss@firma.test'}),
        "subject with newline": dict(subject="=?utf-8?q?Rechnung=0ABcc:_evil@x.test?="),
    }
    for label, change in variants.items():
        s = await _stack()
        try:
            draft = await _forward(s)
            s.api.replace_draft(draft["draft_id"], _raw_draft(**{"to": WEB, "body": draft["body"],
                                                                  **change}))
            result = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
            require(result.outcome is not OUT.APPROVAL_REQUIRED
                    and result.reason in ("draft_not_showable", "draft_recipients_changed"),
                    f"{label}: {result}")
            require_equal(await s.storage.list_pending(), [], f"{label}: the owner was asked")
            require_equal(s.api.sent, [])
        finally:
            await s.storage.close()


async def t_a_draft_with_other_recipients_is_refused_before_anyone_is_asked():
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    for headers in ({"Cc": "eve@example.test"}, {"Bcc": "eve@example.test"},
                    {"To": WEB + ", eve@example.test"}):
        s = await _stack()
        try:
            draft = await _forward(s)
            raw = mime_with_attachment(sender=OWN, to=headers.get("To", WEB),
                                       subject=draft["subject"], body=draft["body"],
                                       files=[("receipt.pdf", "application/pdf", PDF)])
            extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "To")
            s.api.replace_draft(draft["draft_id"], extra.encode() + raw)
            result = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]},
                                 SAID_SEND)
            require(result.outcome is not OUT.APPROVAL_REQUIRED
                    and result.reason == "draft_recipients_changed", f"{headers}: {result}")
            require_equal(await s.storage.list_pending(), [],
                          f"{headers}: the owner was asked about a draft that cannot go out")
            require_equal(s.api.sent, [])
        finally:
            await s.storage.close()


async def t_a_forward_that_cannot_be_complete_or_shown_is_not_drafted():
    from solvio.capabilities import gmail as G
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    s = await _stack()
    try:
        cases = [({"forward_message": "m-receipt", "body": ""}, "missing_recipient"),
                 ({"forward_message": "m-unknown", "to": WEB, "body": ""}, "message_not_found"),
                 ({"forward_message": "m-receipt", "to": WEB, "body": "",
                   "reply_to_message": "m-receipt"}, "forward_and_reply")]
        _receipt(s.api, body="Zeile\n" * 900, message_id="m-long")
        cases.append(({"forward_message": "m-long", "to": WEB, "body": "x" * 4000},
                      "forward_note_too_long"))
        for args, reason in cases:
            result = await _exec(s, "gmail_create_draft", args, SAID_FORWARD)
            require(result.outcome is not OUT.SUCCESS and result.reason == reason,
                    f"{reason}: {result}")
        with patch.object(G, "MAX_FORWARD_BYTES", len(PDF) - 1):
            result = await _exec(s, "gmail_create_draft",
                                 {"forward_message": "m-receipt", "to": WEB, "body": ""},
                                 SAID_FORWARD)
            require_equal(result.reason, "forward_too_large", str(result))
        require_equal(s.api.drafts, {}, "a refused forward still left a draft")
    finally:
        await s.storage.close()



async def t_an_attachment_that_cannot_be_read_stops_forward_and_send():
    """Gmail can carry a named part without an `attachmentId` (inline data). Then the
    forward would silently lose it, and a draft could not be checked byte for byte."""
    from solvio.capabilities.contract import CapabilityRefused
    from solvio.capabilities.gmail import GmailCapabilities
    from solvio.integrations.gmail import EmailMessage

    half = EmailMessage("m-half", "t", sender="a@example.test", to=OWN, subject="S",
                        body="B", attachment_names=("receipt.pdf",), attachments=())

    class Provider:
        async def message(self, message_id):
            return half

        async def get_draft(self, draft_id):
            import base64
            return {"id": draft_id, "message": {"id": "dm", "payload": {
                "mimeType": "multipart/mixed", "headers": [
                    {"name": "To", "value": WEB}, {"name": "Subject", "value": "S"}],
                "body": {"size": 0}, "parts": [
                    {"mimeType": "text/plain", "filename": "", "headers": [],
                     "body": {"data": base64.urlsafe_b64encode(b"B").decode()}},
                    {"mimeType": "application/pdf", "filename": "receipt.pdf", "headers": [],
                     "body": {"data": base64.urlsafe_b64encode(PDF[:10]).decode()}}]}}}

    caps = GmailCapabilities(Provider())
    for call, reason in ((caps.create_draft({"forward_message": "m-half", "to": WEB, "body": ""}),
                          "forward_attachment_unreadable"),
                         (caps.describe_send({"draft_id": "r-1"}), "draft_not_showable")):
        try:
            await call
        except CapabilityRefused as exc:
            require_equal(exc.reason, reason)
        else:
            raise AssertionError(reason + " was not refused")


async def t_a_forward_has_a_bounded_number_of_attachments():
    from solvio.capabilities import gmail as G
    s = await _stack()
    try:
        with patch.object(G, "MAX_FORWARD_ATTACHMENTS", 0):
            result = await _exec(s, "gmail_create_draft",
                                 {"forward_message": "m-receipt", "to": WEB, "body": ""}, SAID_FORWARD)
        require_equal(result.reason, "forward_too_many_attachments", str(result))
        require_equal(s.api.drafts, {})
    finally:
        await s.storage.close()

# =====================================================================
# After Face ID the Core sends — also when the conversation already ended
# =====================================================================

class _Inbox:
    def __init__(self):
        self.items = []

    async def add_item(self, item):
        self.items.append(item)
        return True


async def _continuation(decision: str = "approve"):
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.security.mobile_approval import protocol as PR
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.gmail_capability_tools import gmail_capability_tools
    from solvio.tools.registry import attach_agent_runtime
    s = await _stack()
    draft = await _forward(s)
    gate = _gate(SAID_SEND)
    dispatcher = ToolDispatcher()
    dispatcher.capabilities = s.router
    dispatcher.capability_gate = gate
    for tool in gmail_capability_tools(s.router, gate):
        dispatcher.register(tool)
    ledger = AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=SANDBOX), "runs.sqlite3"))
    inbox = _Inbox()
    attach_agent_runtime(dispatcher, Orchestrator(ledger=ledger, router=s.router,
                                                  control_plane=s.cp, proactive=inbox))
    spoken = await dispatcher.tool("gmail_send_draft").run({"draft_id": draft["draft_id"]})
    require(not spoken.success and "Verschickt ist noch nichts" in spoken.human_message,
            spoken.human_message)
    request_id = spoken.data["request_id"]
    gate.clear()                                   # the conversation ended
    wire, reason = await s.cp.issue_challenge(approval_id=request_id,
                                              device_id=s.device.device_id)
    require(wire is not None, reason)
    _, status = await s.co.apply_mobile_decision(**s.H.sign_decision(
        s.device, PR.b64d(wire["payload_b64"]),
        decision=PR.DECISION_APPROVE if decision == "approve" else PR.DECISION_DENY))
    require_equal(status, "ok")
    runtime = Orchestrator(ledger=AgentRunLedger(ledger.path), router=s.router,
                           control_plane=s.cp, proactive=inbox)
    attach_agent_runtime(dispatcher, runtime)
    await runtime.tick()
    await runtime.tick()
    return s, inbox


async def t_after_face_id_the_core_sends_exactly_once_and_says_so():
    s, inbox = await _continuation()
    try:
        require_equal(len(s.api.sent), 1, "the approved mail did not go out exactly once")
        require_equal(len(inbox.items), 1, f"inbox: {inbox.items}")
        require_equal(inbox.items[0]["summary"],
                      f"Die freigegebene Mail an {WEB} mit 1 Anhang ist verschickt.")
    finally:
        await s.storage.close()


async def t_a_denied_mail_is_never_sent_and_never_asked_again():
    s, inbox = await _continuation("deny")
    try:
        require_equal(s.api.sent, [])
        require_equal(await s.storage.list_pending(), [], "a denial was asked again")
    finally:
        await s.storage.close()



async def t_a_contact_message_waits_for_face_id_and_the_core_sends_it_once():
    """Review S2, Befund 5: `communication_send` braucht vom Telefon jetzt Face ID — ohne
    Merkzettel liefe die erteilte Freigabe ins Leere. Jetzt sendet der Core-Takt genau einmal
    und meldet es; hinaus geht genau der freigegebene Wortlaut."""
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.capabilities.communication import CommunicationCapabilities
    from solvio.capabilities.communication import register as register_contacts
    from solvio.communication.bindings import BindingStore
    from solvio.tools.communication_capability_tools import communication_capability_tools
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.registry import attach_agent_runtime
    s = await _stack()
    try:
        store = BindingStore(os.path.join(tempfile.mkdtemp(dir=SANDBOX), "contacts.sqlite3"))
        store.confirm("mich web", "Gregor", [{"channel": "gmail", "value": WEB}], "test")
        register_contacts(s.router, CommunicationCapabilities(s.caps, store))
        gate = _gate("Schick mir an meine web Adresse: Rechnung liegt im Postfach.")
        dispatcher = ToolDispatcher()
        dispatcher.capabilities = s.router
        dispatcher.capability_gate = gate
        for tool in communication_capability_tools(s.router, gate):
            dispatcher.register(tool)
        ledger = AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=SANDBOX), "runs.sqlite3"))
        inbox = _Inbox()
        attach_agent_runtime(dispatcher, Orchestrator(ledger=ledger, router=s.router,
                                                      control_plane=s.cp, proactive=inbox))
        spoken = await dispatcher.tool("communication_send").run(
            {"alias": "mich web", "channel": "gmail", "recipient_handle": WEB,
             "content": "Rechnung liegt im Postfach."})
        require(not spoken.success and "Verschickt ist noch nichts" in spoken.human_message,
                spoken.human_message)
        gate.clear()
        await sign(s.cp, s.co, s.device, s.H, spoken.data["request_id"], s.counter)
        runtime = Orchestrator(ledger=AgentRunLedger(ledger.path), router=s.router,
                               control_plane=s.cp, proactive=inbox)
        attach_agent_runtime(dispatcher, runtime)
        await runtime.tick()
        await runtime.tick()
        require_equal(len(s.api.sent), 1, "the approved message did not leave exactly once")
        require_equal(sent_message(s.api.sent[0]).get_content().strip(), "Rechnung liegt im Postfach.")
        require_equal([item["summary"] for item in inbox.items],
                      [f"Die freigegebene Mail an {WEB} ist verschickt."])
    finally:
        await s.storage.close()


async def t_a_reply_stays_in_its_thread_and_the_screen_says_what_it_answers():
    """Review S2, finding 3: the rebuilt mail lost In-Reply-To and the thread."""
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    s = await _stack()
    try:
        created = await _exec(s, "gmail_create_draft",
                              {"reply_to_message": "m-receipt", "body": "Danke, ist angekommen."},
                              "Antworte auf die Rechnung: Danke, ist angekommen.")
        require_equal(created.outcome, OUT.SUCCESS, str(created))
        draft_id = created.data["draft_id"]
        first = await _exec(s, "gmail_send_draft", {"draft_id": draft_id}, SAID_SEND)
        shown = (await s.storage.get_request(first.data["request_id"]))["task"]
        require("Antwort auf" in shown and RECEIPT_ID in shown, shown)
        await sign(s.cp, s.co, s.device, s.H, first.data["request_id"], s.counter)
        result = await _exec(s, "gmail_send_draft", {"draft_id": draft_id}, SAID_SEND,
                             approval_id=first.data["request_id"])
        require_equal(result.outcome, OUT.SUCCESS, str(result))
        mail = sent_message(s.api.sent[0])
        require_equal(mail["In-Reply-To"], RECEIPT_ID,
                      "a reply names the RFC Message-ID, not Gmail's object id")
        require_equal(mail["References"], mail["In-Reply-To"])
        require_equal(s.api.sent_threads, ["t-m-receipt"], "the reply left its thread")
    finally:
        await s.storage.close()



async def t_a_mail_that_cannot_be_built_is_reported_as_not_sent():
    """If the approved description cannot be turned into a mail, nothing left — and that is
    what the frozen path must hear (`failed_safe`), not an uncertain outcome."""
    from solvio.capabilities.router import _WithArguments
    from solvio.security.mobile_approval.execution import SafeExecutionFailure
    s = await _stack()
    try:
        draft = await _forward(s)
        approved = await s.caps.describe_send({"draft_id": draft["draft_id"]})

        def broken(**kw):
            raise ValueError("header")

        with patch.object(s.api, "_mime", broken):
            try:
                await _WithArguments(s.caps.send_draft, {"draft_id": draft["draft_id"]})(approved)
            except SafeExecutionFailure as exc:
                require_equal(str(exc), "mail_not_buildable")
            else:
                raise AssertionError("an unbuildable mail was reported otherwise")
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()

async def t_a_contact_message_must_be_visible_and_permitted_before_anyone_is_asked():
    """Review S2, findings 1 and 5: the contact path sends its own wording byte for byte — so
    that wording must be fully visible; and if the vault would refuse, nobody is asked."""
    from solvio.capabilities.communication import CommunicationCapabilities
    from solvio.capabilities.communication import register as register_contacts
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    from solvio.capabilities.gmail import GmailCapabilities, MAX_SENDABLE_BODY
    from solvio.communication.bindings import BindingStore

    class Vault:
        def __init__(self, allowed):
            self.allowed = allowed

        def describe(self, ref):
            return {"status": "active", "allowed_capabilities": list(self.allowed)}

    for label, content, vault, reason in (
            ("zero-width", "Das Essen ist fertig." + "\u200b\u200c\u2060" * 50, None,
             "content_not_showable"),
            ("blank-looking", "Das Essen ist fertig." + "\u3164" * 40, None, "content_not_showable"),
            ("too long", "x" * (MAX_SENDABLE_BODY + 1), None, "content_not_showable"),
            ("vault scope", "Das Essen ist fertig.", Vault({"gmail_send_draft"}),
             "contact_send_not_permitted_by_vault")):
        s = await _stack()
        try:
            api = GmailApi(broker=vault)
            store = BindingStore(os.path.join(tempfile.mkdtemp(dir=SANDBOX), "contacts.sqlite3"))
            store.confirm("mein Sohn", "Adam", [{"channel": "gmail", "value": WEB}], "test")
            register_contacts(s.router, CommunicationCapabilities(GmailCapabilities(api), store))
            result = await _exec(s, "communication_send",
                                 {"alias": "mein Sohn", "channel": "gmail",
                                  "recipient_handle": WEB, "content": content},
                                 "Sag meinem Sohn Bescheid.")
            require(result.outcome is not OUT.APPROVAL_REQUIRED and result.reason == reason,
                    f"{label}: {result}")
            require_equal(await s.storage.list_pending(), [], f"{label}: the owner was asked")
            require_equal(api.sent, [])
        finally:
            await s.storage.close()

def t_the_report_never_claims_more_than_gmail_confirmed():
    from solvio.agent_runtime.orchestrator import Orchestrator
    sentence = Orchestrator._mail_outcome_sentence
    ok = SimpleNamespace(data={"sent": True, "message_id": "x", "to": WEB, "attachments": 2})
    require_equal(sentence(ok, True), (f"Die freigegebene Mail an {WEB} mit 2 Anhängen ist verschickt.", True))
    unconfirmed = SimpleNamespace(data={"sent": False, "message_id": "", "to": WEB})
    text, sent = sentence(unconfirmed, True)
    require(not sent and "nicht sicher" in text, text)
    for reason in ("denied", "approval_drift", "failed_safe", "unknown_approval"):
        nothing = SimpleNamespace(data=None, reason=reason, had_no_effect=True,
                                  human_message="Der Entwurf ist weg.")
        require_equal(sentence(nothing, False),
                      ("Die freigegebene Mail wurde nicht verschickt. Der Entwurf ist weg.", False))
    # Review S2, Befund 7: nach der Sendegrenze gescheitert (Journal, Antwort verloren) —
    # das ist KEIN „nicht verschickt", auch wenn der Umschlag keine Wirkung vermutet.
    # `not_approved` also covers EXECUTING/CONSUMED — another call may have sent it (finding 2).
    for reason in ("capability_failed", "unknown_outcome", "outcome_lost", "not_approved", ""):
        after = SimpleNamespace(data=None, reason=reason, had_no_effect=True, human_message="x")
        text, sent = sentence(after, False)
        require(not sent and "nicht sicher" in text, f"{reason}: {text}")
    unknown = SimpleNamespace(data=None, had_no_effect=False, human_message="")
    text, sent = sentence(unknown, False)
    require(not sent and "Gesendet" in text, text)
    text, sent = sentence(None, False)
    require(not sent and "nicht sicher" in text, text)


def t_the_voice_tool_asks_only_for_the_draft_and_the_model_is_told_the_truth():
    from solvio.realtime.live_session import LIVE_TOOL_INSTRUCTIONS
    from solvio.tools.gmail_capability_tools import _SCHEMAS
    from solvio.tools.agent_capability_tools import CONTINUABLE_CAPABILITIES
    from solvio.agent_runtime.steps import RESUMABLE_STARTS
    require_equal(_SCHEMAS["gmail_send_draft"]["parameters"]["required"], ["draft_id"])
    require("forward_message" in _SCHEMAS["gmail_create_draft"]["parameters"]["properties"])
    require("gmail_send_draft" in CONTINUABLE_CAPABILITIES and "gmail_send_draft" in RESUMABLE_STARTS)
    require("mail_forward" in LIVE_TOOL_INSTRUCTIONS
            and "immer Face ID" in LIVE_TOOL_INSTRUCTIONS, LIVE_TOOL_INSTRUCTIONS)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
