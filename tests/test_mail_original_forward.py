"""DEBT-0338: original-file fallback, real MIME and approval chain; synthetic mail only."""
from __future__ import annotations

import base64
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from mail_send_harness import sign, sent_message
from test_mail_send_face_id import _stack, _receipt, _forward, _exec, _raw_draft, WEB, SAID_SEND, PDF
from solvio.capabilities.envelope import CapabilityOutcome as OUT


async def t_long_original_including_headers_html_and_files_leaves_byte_exact_only_after_face_id():
    from email.message import EmailMessage
    s = await _stack()
    try:
        original = EmailMessage()
        original["From"] = "sender@example.test"
        original["To"] = "owner@example.test"
        original["Bcc"] = "old-recipient@example.test"
        original["Subject"] = "Synthetic long mail"
        original.set_content("Long private original line\n" * 300)
        original.add_alternative("<p>Original HTML, not an outer mail body</p>", subtype="html")
        original.add_attachment(PDF, maintype="application", subtype="pdf", filename="receipt.pdf")
        raw = original.as_bytes()
        s.api.inbox["m-receipt"] = raw
        draft = await _forward(s, body="Here is the requested original.")
        require("Long private original" not in str(draft), "original leaked to tool result")
        require_equal(len(draft["attachments"]), 1)
        first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
        require_equal(first.outcome, OUT.APPROVAL_REQUIRED)
        shown = (await s.storage.get_request(first.data["request_id"]))["task"]
        for expected in (WEB, "Originalmail.eml", "Kopfzeilen und Anhaenge",
                         "nicht als Text angezeigt", "Here is the requested original."):
            require(expected in shown, expected)
        require("Long private original" not in shown, "raw original became approval body")
        require_equal(s.api.sent, [])
        await sign(s.cp, s.co, s.device, s.H, first.data["request_id"], s.counter)
        sent = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                           approval_id=first.data["request_id"])
        require_equal(sent.outcome, OUT.SUCCESS)
        require_equal(len(s.api.sent), 1)
        mail = sent_message(s.api.sent[0])
        require_equal(mail["To"], WEB)
        require_equal(mail["Bcc"], None, "original Bcc became outer recipient")
        files = list(mail.iter_attachments())
        require_equal(len(files), 1)
        require_equal(files[0].get_filename(), "Originalmail.eml")
        require_equal(files[0].get_payload(decode=True), raw)
        require_equal(files[0].get_content_type(), "application/octet-stream")
    finally:
        await s.storage.close()


async def t_changed_original_file_cannot_use_previous_face_id():
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        raw = s.api.inbox["m-receipt"]
        draft = await _forward(s)
        first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
        await sign(s.cp, s.co, s.device, s.H, first.data["request_id"], s.counter)
        s.api.replace_draft(draft["draft_id"], _raw_draft(
            to=WEB, body=draft["body"], subject=draft["subject"],
            files=[("Originalmail.eml", "application/octet-stream", raw.replace(b"Line", b"Evil"))]))
        sent = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                           approval_id=first.data["request_id"])
        require(sent.outcome is not OUT.SUCCESS, "changed original accepted")
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()


async def t_long_forward_does_not_fetch_individual_parts_or_truncate_large_note():
    from solvio.capabilities.contract import CapabilityRefused
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        await _forward(s)
        require(not any("/attachments/" in path for _, path in s.api.calls),
                "original file must include parts without a second download/reconstruction")
        before = len(s.api.drafts)
        try:
            await s.caps.create_draft({"forward_message": "m-receipt", "to": WEB, "body": "x" * 4000})
        except CapabilityRefused as exc:
            require_equal(exc.reason, "forward_note_too_long")
        else:
            raise AssertionError("long note was silently shortened")
        require_equal(len(s.api.drafts), before)
    finally:
        await s.storage.close()


async def t_original_size_limit_refuses_before_creating_draft():
    from solvio.capabilities import gmail as G
    from solvio.capabilities.contract import CapabilityRefused
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        with patch.object(G, "MAX_FORWARD_BYTES", len(s.api.inbox["m-receipt"]) - 1):
            try:
                await s.caps.create_draft({"forward_message": "m-receipt", "to": WEB, "body": ""})
            except CapabilityRefused as exc:
                require_equal(exc.reason, "forward_too_large")
            else:
                raise AssertionError("oversize file accepted")
        require_equal(s.api.drafts, {})
    finally:
        await s.storage.close()


async def t_raw_gmail_contract_strict_decoding_identity_and_size():
    from solvio.integrations.gmail import Gmail, GmailMessageTooLarge
    class Api(Gmail):
        async def _request(self, method, path, *, params=None):
            require_equal((method, path, params), ("GET", "/messages/m", {"format": "raw"}))
            return self.response
    api = Api(client_id="synthetic")
    for response in (None, {}, {"id": "other", "raw": "YQ=="}, {"id": "m", "raw": "%%%"},
                     {"id": "m", "raw": ""}, {"id": "m", "raw": "\u00e4"}):
        api.response = response
        require_equal(await api.raw_message("m", max_bytes=30), None)
    api.response = {"id": "m", "raw": base64.urlsafe_b64encode(b"exact\r\n\xff").decode().rstrip("=")}
    require_equal(await api.raw_message("m", max_bytes=8), b"exact\r\n\xff")
    for limit in (7, 1):
        try:
            await api.raw_message("m", max_bytes=limit)
        except GmailMessageTooLarge:
            pass
        else:
            raise AssertionError("size limit ignored")


async def t_unavailable_original_creates_no_partial_draft_and_no_approval():
    from solvio.capabilities.contract import CapabilityRefused
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        async def unavailable(*args, **kwargs):
            return None
        with patch.object(s.api, "raw_message", unavailable):
            try:
                await s.caps.create_draft({"forward_message": "m-receipt", "to": WEB, "body": ""})
            except CapabilityRefused as exc:
                require_equal(exc.reason, "forward_original_unreadable")
            else:
                raise AssertionError("missing original accepted")
        require_equal((s.api.drafts, s.api.sent, await s.storage.list_pending()), ({}, [], []))
    finally:
        await s.storage.close()


async def t_source_ended_while_fetching_original_leaves_no_draft():
    from solvio.capabilities.gmail import mail_source_scope
    from solvio.security.mobile_approval.execution import SafeExecutionFailure
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        async def ended():
            return False
        with mail_source_scope(ended):
            try:
                await s.caps.create_draft({"forward_message": "m-receipt", "to": WEB, "body": ""})
            except SafeExecutionFailure:
                pass
            else:
                raise AssertionError("ended source still created draft")
        require_equal(s.api.drafts, {})
    finally:
        await s.storage.close()


async def t_denied_original_forward_stays_denied_without_alternative_send():
    from solvio.security.mobile_approval import protocol as PR
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        draft = await _forward(s)
        first = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND)
        request_id = first.data["request_id"]
        wire, _ = await s.cp.issue_challenge(approval_id=request_id, device_id=s.device.device_id)
        s.counter[0] += 1
        _, status = await s.co.apply_mobile_decision(**s.H.sign_decision(
            s.device, PR.b64d(wire["payload_b64"]), decision=PR.DECISION_DENY, counter=s.counter[0]))
        require_equal(status, "ok")
        for _ in range(2):
            denied = await _exec(s, "gmail_send_draft", {"draft_id": draft["draft_id"]}, SAID_SEND,
                                approval_id=request_id)
            require(denied.outcome is not OUT.SUCCESS, "denial lost")
        require_equal((len(s.api.drafts), s.api.sent, await s.storage.list_pending()), (1, [], []))
    finally:
        await s.storage.close()


async def t_parsed_eml_children_never_become_outer_text_or_extra_files():
    from solvio.integrations.gmail import message_from_api
    from solvio.capabilities.gmail import _draft_parts
    s = await _stack()
    try:
        _receipt(s.api, body="Nested private line\n" * 400)
        draft = await _forward(s, body="Outer note")
        stored = await s.api.get_draft(draft["draft_id"])
        payload = stored["message"]["payload"]
        outer_file = payload["parts"][1]
        require(bool(outer_file.get("parts")), "fixture must reproduce Gmail's expanded .eml")
        require(bool(outer_file["body"].get("attachmentId")), "whole file remains fetchable")
        parsed = message_from_api(stored["message"])
        require_equal(parsed.body, draft["body"])
        require_equal(parsed.attachment_names, ("Originalmail.eml",))
        require_equal(len(parsed.attachments), 1, "inner PDF became an outer attachment")
        require_equal(_draft_parts(payload).strip(), draft["body"])
        description = await s.caps.describe_send({"draft_id": draft["draft_id"]})
        require_equal(description["body"], draft["body"])
        require_equal(description["attachments"], draft["attachments"])
    finally:
        await s.storage.close()


async def t_named_eml_without_whole_file_bytes_still_refuses_even_with_readable_children():
    from solvio.capabilities.contract import CapabilityRefused
    from solvio.integrations.gmail import message_from_api
    from solvio.capabilities.gmail import _draft_parts
    s = await _stack()
    try:
        _receipt(s.api, body="Line\n" * 1000)
        draft = await _forward(s)
        stored = await s.api.get_draft(draft["draft_id"])
        part = stored["message"]["payload"]["parts"][1]
        require(bool(part.get("parts")), "expanded file missing from fixture")
        part["body"].pop("attachmentId")
        require_equal(_draft_parts(stored["message"]["payload"]), None)
        parsed = message_from_api(stored["message"])
        require_equal(parsed.attachment_names, ("Originalmail.eml",))
        require_equal(parsed.attachments, ())
        async def unreadable(_): return stored
        with patch.object(s.api, "get_draft", unreadable):
            try:
                await s.caps.describe_send({"draft_id": draft["draft_id"]})
            except CapabilityRefused as exc:
                require_equal(exc.reason, "draft_not_showable")
            else:
                raise AssertionError("child parts replaced unreadable original")
        require_equal((s.api.sent, await s.storage.list_pending()), ([], []))
    finally:
        await s.storage.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
