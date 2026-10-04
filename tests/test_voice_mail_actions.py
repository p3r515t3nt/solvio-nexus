"""Stage S2 after the first live run (27.09.2026): mail from the voice path in one step.

Measured in the owner's run: the Live selector sees per utterance only text and history,
never the ids in a search result. `gmail_create_draft(forward_message=...)` was out of its
reach; it searched nine times, nothing was drafted, no Face ID request came — and the
speaking model said "Erledigt. Die Mail ist jetzt rausgegangen." These tests drive the
one-step tools over the real router, the real approval chain (synthetic device only) and the
real Gmail client over the in-memory Gmail REST surface, and the send-claim backstop.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from mail_send_harness import (  # noqa: E402
    OWN, GmailApi, approval_chain, mime_with_attachment, sent_message, sign)

SANDBOX = tempfile.mkdtemp(prefix="solvio-voice-mail-")
WEB = "gregor-brawanski@web.test"
JUNE_PDF = b"%PDF-1.4 june receipt" * 20
SEPT_PDF = b"%PDF-1.4 september receipt" * 20
SAID = "Leite die letzte Rechnung von ElevenLabs an gregor minus brawanski at web punkt test weiter."


FAKE_PDF = b"%PDF-1.4 pay to attacker" * 20


def _inbox(api: GmailApi) -> None:
    # Filled in RECEIVE order. The first one is an attacker's look-alike with a Date header in
    # 2099 (review S2 voice path, finding 4): it must never count as "the newest".
    for message_id, sender, date, pdf in (
            ("m-fake", "ElevenLabs Billing <billing@elevenlabs-invoices.test>",
             "Thu, 31 Dec 2099 09:00:00 +0000", FAKE_PDF),
            ("m-june", "ElevenLabs <billing@elevenlabs.test>", "Mon, 30 Jun 2026 09:00:00 +0000", JUNE_PDF),
            ("m-sept", "ElevenLabs <billing@elevenlabs.test>", "Mon, 15 Sep 2026 09:00:00 +0000", SEPT_PDF)):
        api.inbox[message_id] = mime_with_attachment(
            sender=sender, to=OWN, date=date, message_id=f"<{message_id}@elevenlabs.test>",
            subject="Your receipt from ElevenLabs Inc.", body="Amount: 22.00 USD",
            files=[("receipt.pdf", "application/pdf", pdf)])


def _gate(said: str = SAID, origin=None):
    from solvio.capabilities import policy as P
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.contracts.trust import TrustContext, TrustLevel
    gate = CapabilityInvocationGate()
    gate.begin_turn(session_id="s-voice", turn_id="s-voice-t1", principal="iphone-dev-test",
                    trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
                                       note="Sprachturn"),
                    user_text=said, origin=origin or P.OriginClass.TRUSTED_INTERACTIVE_APP)
    return gate


async def _stack(gate=None):
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.gmail import GmailCapabilities, register
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.gmail_capability_tools import gmail_capability_tools
    cp, co, device, H, storage = await approval_chain(SANDBOX)
    router = CapabilityRouter(mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                              policy_mode="enforce")
    api = GmailApi()
    _inbox(api)
    register(router, GmailCapabilities(api))
    gate = gate or _gate()
    dispatcher = ToolDispatcher()
    dispatcher.capabilities = router
    dispatcher.capability_gate = gate
    for tool in gmail_capability_tools(router, gate):
        dispatcher.register(tool)
    return SimpleNamespace(cp=cp, co=co, device=device, H=H, storage=storage, router=router,
                           api=api, gate=gate, dispatcher=dispatcher, counter=[0])


class _Inbox:
    def __init__(self):
        self.items = []

    async def add_item(self, item):
        self.items.append(item)
        return True


async def t_mail_forward_takes_the_newest_match_asks_face_id_and_sends_once_after_it():
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.tools.registry import attach_agent_runtime
    s = await _stack()
    try:
        ledger = AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=SANDBOX), "runs.sqlite3"))
        inbox = _Inbox()
        attach_agent_runtime(s.dispatcher, Orchestrator(ledger=ledger, router=s.router,
                                                        control_plane=s.cp, proactive=inbox))
        spoken = await s.dispatcher.tool("mail_forward").run(
            {"query": "from:elevenlabs receipt", "to": WEB})
        require(not spoken.success and spoken.error == "approval_required", str(spoken))
        for part in ("billing@elevenlabs.test", "Betreff laut Mail", WEB,
                     "Freigabe liegt auf deinem iPhone", "Verschickt ist noch nichts"):
            require(part in spoken.human_message, f"{part!r} missing: {spoken.human_message}")
        require_equal(s.api.sent, [], "sent before Face ID")
        pending = await s.storage.list_pending()
        require_equal(len(pending), 1, "exactly one Face ID request")
        shown = pending[0]["task"]
        require("15 Sep 2026" in shown and "30 Jun 2026" not in shown and "2099" not in shown,
                f"the screen must show the September mail:\n{shown}")

        s.gate.clear()                      # the conversation may end
        await sign(s.cp, s.co, s.device, s.H, spoken.data["request_id"], s.counter)
        runtime = Orchestrator(ledger=AgentRunLedger(ledger.path), router=s.router,
                               control_plane=s.cp, proactive=inbox)
        attach_agent_runtime(s.dispatcher, runtime)
        await runtime.tick()
        await runtime.tick()
        require_equal(len(s.api.sent), 1, "the approved mail did not leave exactly once")
        files = [p.get_payload(decode=True) for p in sent_message(s.api.sent[0]).iter_attachments()]
        require_equal(files, [SEPT_PDF], "not the newest receipt")
        require_equal([i["summary"] for i in inbox.items],
                      [f"Die freigegebene Mail an {WEB} mit 1 Anhang ist verschickt."])
        require_equal(len(runtime.confirmed_mail_sends), 1,
                      "the voice backstop gets no evidence of the real send")
    finally:
        await s.storage.close()


async def t_mail_forward_without_a_match_prepares_nothing_and_says_so():
    s = await _stack()
    try:
        spoken = await s.dispatcher.tool("mail_forward").run({"query": "from:nobody", "to": WEB})
        require(not spoken.success and "Verschickt ist nichts" in spoken.human_message, str(spoken))
        require_equal((s.api.drafts, await s.storage.list_pending()), ({}, []))
    finally:
        await s.storage.close()


async def t_a_long_spoken_forward_uses_original_file_and_waits_for_face_id():
    s = await _stack()
    try:
        s.api.inbox["m-long"] = mime_with_attachment(
            sender="ElevenLabs <billing@elevenlabs.test>", to=OWN, subject="Newsletter",
            body="Zeile\n" * 900, files=[], date="Tue, 16 Sep 2026 09:00:00 +0000")
        spoken = await s.dispatcher.tool("mail_forward").run({"query": "newsletter", "to": WEB})
        require(not spoken.success and spoken.error == "approval_required", str(spoken))
        pending = await s.storage.list_pending()
        require_equal(len(pending), 1)
        require("Originalmail.eml" in pending[0]["task"], pending[0]["task"])
        require("nicht als Text angezeigt" in pending[0]["task"], pending[0]["task"])
        require("vollstaendig als Originalmail-Anhang" in spoken.human_message, str(spoken))
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()


async def t_mail_send_prepares_a_new_mail_for_face_id():
    s = await _stack(_gate("Schreib gregor minus brawanski at web punkt test: Ich komme um acht."))
    try:
        spoken = await s.dispatcher.tool("mail_send").run(
            {"to": WEB, "subject": "Heute", "body": "Ich komme um acht."})
        require(not spoken.success and spoken.error == "approval_required", str(spoken))
        shown = (await s.storage.list_pending())[0]["task"]
        require("Ich komme um acht." in shown and WEB in shown, shown)
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()


def t_the_voice_selector_gets_the_one_step_tools_not_the_single_steps():
    from solvio.capabilities.router import CapabilityRouter
    from solvio.realtime.live_session import LIVE_TOOL_INSTRUCTIONS, live_toolkit
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.gmail_capability_tools import gmail_capability_tools
    dispatcher = ToolDispatcher()
    for tool in gmail_capability_tools(CapabilityRouter(), None):
        dispatcher.register(tool)
    names = {tool["name"] for tool in live_toolkit(dispatcher)}
    require({"mail_forward", "mail_send", "gmail_search"} <= names, names)
    require(not {"gmail_create_draft", "gmail_send_draft"} & names, names)
    require("mail_forward" in LIVE_TOOL_INSTRUCTIONS and "immer Face ID" in LIVE_TOOL_INSTRUCTIONS)


def t_the_send_claim_detector_flags_claims_not_questions_or_denials():
    """Narrow on purpose (review S2 voice path, finding 2): SOLVIO's own act on a mail."""
    from solvio.realtime.live_session import unbacked_send_claim
    for text in ("Erledigt. Die Mail ist jetzt rausgegangen.", "Ich habe sie weitergeleitet.",
                 "Die Mail wurde an Gregor verschickt.", "Ich habe sie noch schnell verschickt.",
                 "Ich habe die Mail an Gregor geschickt.", "Die Mail ging raus.",
                 "Die Nachricht ist unterwegs.", "Erledigt, verschickt!", "Gut. Sie ist raus.",
                 "Keine Sorge, die Mail ist verschickt.", "Kein Problem, ich habe sie weitergeleitet."):
        require(unbacked_send_claim(text), f"missed: {text}")
    for text in ("Soll ich sie weiterleiten?", "Ist sie schon raus?", "Ich leite sie gleich weiter.",
                 "Die Freigabe liegt auf deinem iPhone. Verschickt ist noch nichts.",
                 "Die Mail ist noch nicht verschickt.", "Ich habe die Mail vorbereitet.",
                 "Amazon hat dein Paket verschickt.", "Laut der Mail ist dein Paket versandt worden.",
                 "Ich habe das an den Rechercheagenten weitergeleitet.",
                 "Ich habe die Freigabeanfrage an dein iPhone gesendet.", "Die neuen Zahlen sind raus.",
                 "Ich habe dir die Freigabe geschickt.", "Die Notiz ist gespeichert."):
        require(not unbacked_send_claim(text), f"false alarm: {text}")


def _session():
    """A LiveSession with only what the transcript path touches — driven through the REAL
    `_accept_transcript` with provider-shaped events (review finding: the backstop was
    unwired in a mutation and every suite stayed green)."""
    from solvio.realtime import live_session as LS
    session = object.__new__(LS.LiveSession)
    session.session_id = "s-guard"
    session._seen_fragments, session._input_parts, session._assistant_parts = set(), [], []
    session._input_message_id, session._transcript_role = "", None
    session._spoken_since_user, session._claim_corrected = "", False
    session._mail_request, session._corrections = None, set()
    session._consumed_end, session._revision, session._late_input = -1.0, 0, False
    session._read_continuation, session.turn_user_text = None, ""
    session.turn_text_ready, session._input_changed = asyncio.Event(), asyncio.Event()
    session._input_changed_at = 0.0
    session.runtime = SimpleNamespace(confirmed_mail_sends=[])
    session.server = SimpleNamespace(dispatcher=SimpleNamespace(capability_gate=None,
                                                                agent_runtime=session.runtime))
    session.touch = lambda: None
    session._offer_memory_intent = lambda *a, **k: None
    session._persist_input = lambda: ""
    session._persist_assistant = lambda: session._assistant_parts.clear()
    session._persist_remaining_context = lambda: None
    session._deliverable = lambda: True
    session.sent = []

    async def update(kind, text, delegation_id=None):
        session.sent.append(text)

    session._update = update
    session.counter = [0]

    def say(role, text):
        session.counter[0] += 1
        kind = "session.output_transcript.delta" if role == "assistant" else "session.input_transcript.delta"
        session._accept_transcript({"type": kind, "delta": text, "start_ms": session.counter[0],
                                    "end_ms": session.counter[0] + 1,
                                    "event_id": f"ev-{session.counter[0]}"})

    session.say = say
    return session


async def _settle(session):
    await asyncio.sleep(0)
    await asyncio.gather(*list(session._corrections))


def _corrections(session):
    return [t for t in session.sent if "Korrektur" in t]


async def t_an_unbacked_send_claim_is_corrected_through_the_real_transcript_path():
    from solvio.realtime import live_session as LS
    s = _session()
    s.say("user", "Leite sie bitte weiter.")
    s.say("assistant", "Erledigt. Die Mail ist jetzt ")
    s.say("assistant", "rausgegangen.")
    s.say("assistant", " Sie ist raus.")
    await _settle(s)
    require_equal(len(_corrections(s)), 1, f"exactly one correction per answer: {s.sent}")
    text = _corrections(s)[0]
    require("kein Beleg" in text and "keine Mail verschickt" not in text,
            "the correction must never claim 'not sent' — it only knows there is no evidence")
    # The next user utterance re-arms the backstop (review mutation m6).
    s.say("user", "Und die andere?")
    s.say("assistant", "Ich habe sie weitergeleitet.")
    await _settle(s)
    require_equal(len(_corrections(s)), 2, s.sent)


async def t_core_evidence_silences_the_backstop_and_a_subject_line_does_not():
    from solvio.realtime import live_session as LS
    import time
    s = _session()
    s.say("user", "Ist sie raus?")
    await s._send_result_text("Betreff: Deine Bestellung ist verschickt")      # foreign text
    await s._send_result_text(LS.MAIL_SENT_PREFIX + WEB + " wartet noch.")     # prefix alone
    s.say("assistant", "Ja, die Mail ist verschickt.")
    await _settle(s)
    require_equal(len(_corrections(s)), 1, "neither a subject line nor the bare prefix is evidence")

    s = _session()
    s.say("user", "Ist sie raus?")
    s._tool_results = [{"error": "approval_required", "data": {"request_id": "ap-ours"}}]
    s._remember_mail_request([{"name": "mail_send"}], SimpleNamespace(principal="owner"))
    s.runtime.confirmed_mail_sends.append({"request_id": "ap-ours", "principal": "owner", "at": time.time()})
    s.say("assistant", "Ja, die Mail ist verschickt.")
    await _settle(s)
    require_equal(_corrections(s), [], "a mail the Core really sent was 'corrected'")
    # A backed claim does not linger: once the evidence is old, later unrelated speech in the
    # same answer must not re-trigger a correction (review finding 8).
    s.runtime.confirmed_mail_sends.clear()
    s.say("assistant", " Sonst noch etwas?")
    s.say("assistant", " Ich bin da.")
    await _settle(s)
    require_equal(_corrections(s), [], s.sent)

    s = _session()
    s.say("user", "Ist sie raus?")
    await s._send_result_text(LS.MAIL_SENT_PREFIX + WEB + " mit 1 Anhang ist verschickt.")
    s.say("assistant", "Ja, die Mail ist verschickt.")
    await _settle(s)
    require_equal(len(_corrections(s)), 1, "a plain text notice is not a bound send receipt")


async def t_outside_the_app_or_for_a_question_nothing_is_prepared_and_it_says_so():
    """Review S2 voice path, finding 3: from the room or the dashboard even the draft needs
    Face ID, and that request showed only an id and had no continuation — a dead end."""
    from solvio.capabilities import policy as P
    for origin, said in ((P.OriginClass.ROOM_VOICE, SAID), (P.OriginClass.TRUSTED_DASHBOARD, SAID),
                         (None, "Leitest du die Rechnung an gregor minus brawanski at web punkt test weiter?")):
        s = await _stack(_gate(said, origin))
        try:
            spoken = await s.dispatcher.tool("mail_forward").run({"query": "from:elevenlabs", "to": WEB})
            require(not spoken.success and "nichts passiert" in spoken.human_message, str(spoken))
            require_equal((s.api.drafts, await s.storage.list_pending(), s.api.calls),
                          ({}, [], []), f"{origin}: something was touched")
        finally:
            await s.storage.close()


async def t_after_a_denial_a_different_mail_is_a_new_question():
    """Review S2 voice path, finding 6 (pre-existing router order): a 'no' to mail 1 made
    mail 2 answer 'Das hast du abgelehnt'. A denial is final for THAT action only."""
    from solvio.security.mobile_approval import protocol as PR
    s = await _stack(_gate("Schreib gregor minus brawanski at web punkt test: Ich komme um acht."))
    try:
        first = await s.dispatcher.tool("mail_send").run({"to": WEB, "subject": "A", "body": "Ich komme um acht."})
        wire, reason = await s.cp.issue_challenge(approval_id=first.data["request_id"],
                                                  device_id=s.device.device_id)
        s.counter[0] += 1
        _res, status = await s.co.apply_mobile_decision(**s.H.sign_decision(
            s.device, PR.b64d(wire["payload_b64"]), decision=PR.DECISION_DENY, counter=s.counter[0]))
        require_equal(status, "ok")
        old = s.gate.context()
        s.gate.begin_turn(session_id=old.session_id, turn_id="s-voice-new-owner-order",
                          principal=old.principal, trust=old.trust, origin=old.origin,
                          user_text="Schreib gregor minus brawanski at web punkt test: Ich komme um neun.")
        second = await s.dispatcher.tool("mail_send").run({"to": WEB, "subject": "B", "body": "Ich komme um neun."})
        require(second.error == "approval_required" and "abgelehnt" not in second.human_message, str(second))
        require(first.data["draft_id"] != second.data["draft_id"], "a new turn must create its own draft")
        from solvio.security.mobile_approval import store as AS
        require_equal((await s.storage.get_request(first.data["request_id"]))["state"], AS.DENIED,
                      "the original denial must stay final")
        require_equal([p["approval_id"] for p in await s.storage.list_pending()], [second.data["request_id"]])
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
