"""Shared harness for mail sending (stage S2, ADR-0041).

Two parts, both built so that the code under test runs as in production:

* `GmailApi` subclasses the REAL Gmail client and replaces only `_request`, the
  HTTP boundary. Drafts are stored as the raw MIME the real `_mime` produced, and
  read back in Gmail's own `format=full` shape: headers, nested parts, text as
  base64url `data`, attachments ONLY as `attachmentId` + `size` (fetched through
  `/messages/{id}/attachments/{aid}`). A fake that returned tidy dicts would hide
  exactly the parsing this stage depends on (lesson of 19.09.2026: fakes must
  follow the provider's rules).
* `approval_chain()` is the real mobile approval chain with a synthetic device —
  control plane, request, challenge, digest binding and single use are
  production code; only WHO signs is simulated (same pattern as the note suites).
"""
from __future__ import annotations

import base64
import email
import email.utils
from email import policy as email_policy
from email.message import EmailMessage as MimeMessage
import os
import tempfile

from solvio.integrations.gmail import Gmail

OWN = "owner@example.test"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def mime_with_attachment(*, sender: str, to: str, subject: str, body: str,
                         files: list[tuple[str, str, bytes]], message_id: str = "",
                         date: str = "Thu, 25 Sep 2026 10:00:00 +0000") -> bytes:
    message = MimeMessage()
    if message_id:
        message["Message-ID"] = message_id
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = date
    message.set_content(body)
    for name, mime_type, data in files:
        maintype, subtype = mime_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


class GmailApi(Gmail):
    """The real client over an in-memory Gmail REST surface."""

    def __init__(self, *, send_ambiguous: bool = False, broker=None) -> None:
        super().__init__(client_id="test-client", broker=broker)
        self.inbox: dict[str, bytes] = {}          # message id -> raw MIME
        self.drafts: dict[str, dict] = {}          # draft id -> {"message_id", "raw"}
        self.sent: list[bytes] = []                # raw MIME of every sent draft
        self.sent_threads: list[str] = []          # threadId named in each send
        self.attachments: dict[tuple[str, str], bytes] = {}
        self.send_ambiguous = send_ambiguous
        self.calls: list[tuple[str, str]] = []
        #: Optional hook (method, path) -> None, run BEFORE each answer: lets a test
        #: change the draft at Gmail at an exact moment ("a second client").
        self.before_response = None
        self._n = 0

    # -- the Gmail format=full shape ----------------------------------------
    def _part(self, message_id: str, part) -> dict:
        headers = [{"name": k, "value": str(v)} for k, v in part.items()]
        out = {"mimeType": part.get_content_type(), "headers": headers,
               "filename": part.get_filename() or ""}
        if part.is_multipart():
            out["body"] = {"size": 0}
            out["parts"] = [self._part(message_id, p) for p in part.iter_parts()]
            return out
        data = part.get_payload(decode=True) or b""
        if out["filename"]:
            aid = f"att-{len(self.attachments) + 1}"
            self.attachments[(message_id, aid)] = data
            out["body"] = {"attachmentId": aid, "size": len(data)}
            # Actual Gmail shape measured on 28.09.2026: even an octet-stream
            # .eml with its own attachmentId also has parsed child parts. They
            # describe the FILE, not additional outer message content.
            if out["filename"].lower().endswith(".eml"):
                embedded = email.message_from_bytes(data, policy=email_policy.default)
                out["parts"] = [self._part(message_id, embedded)]
        else:
            out["body"] = {"data": b64url(data), "size": len(data)}
        return out

    def _full(self, message_id: str, raw: bytes) -> dict:
        parsed = email.message_from_bytes(raw, policy=email_policy.default)
        return {"id": message_id, "threadId": "t-" + message_id, "labelIds": [],
                "snippet": "", "payload": self._part(message_id, parsed)}

    async def _request(self, method, path, *, params=None, json_body=None):
        self.calls.append((method, path))
        if self.before_response is not None:
            self.before_response(method, path)
        if method == "POST" and path == "/drafts":
            self._n += 1
            draft_id, message_id = f"r-{self._n}", f"dm-{self._n}"
            raw = base64.urlsafe_b64decode(json_body["message"]["raw"])
            self.drafts[draft_id] = {"message_id": message_id, "raw": raw,
                                     "thread": json_body["message"].get("threadId", "")}
            return {"id": draft_id, "message": {"id": message_id}}
        if method == "GET" and path.startswith("/drafts/"):
            entry = self.drafts.get(path.rsplit("/", 1)[1])
            if entry is None:
                return None
            full = self._full(entry["message_id"], entry["raw"])
            if entry.get("thread"):
                full["threadId"] = entry["thread"]
            return {"id": path.rsplit("/", 1)[1], "message": full}
        if method == "POST" and path == "/drafts/send":
            if self.send_ambiguous:
                raise TimeoutError()
            entry = self.drafts.pop(json_body["id"])
            # Gmail's documented update-and-send form: with `message.raw` exactly those
            # bytes leave, whatever the stored draft holds; without it, the stored draft.
            update = (json_body.get("message") or {}).get("raw")
            self.sent.append(base64.urlsafe_b64decode(update) if update else entry["raw"])
            self.sent_threads.append((json_body.get("message") or {}).get("threadId", ""))
            return {"id": f"sent-{len(self.sent)}"}
        if method == "GET" and "/attachments/" in path:
            _, _, message_id, _, aid = path.split("/")
            data = self.attachments.get((message_id, aid))
            return None if data is None else {"data": b64url(data)}
        if method == "GET" and path == "/messages":
            # Gmail's list: ids only, newest first, `q` with from: and plain words.
            words = str((params or {}).get("q") or "").lower().split()
            hits = []
            for message_id, raw in self.inbox.items():
                parsed = email.message_from_bytes(raw, policy=email_policy.default)
                text = " ".join([str(parsed.get("From", "")), str(parsed.get("Subject", "")),
                                 parsed.get_body(preferencelist=("plain",)).get_content()
                                 if parsed.get_body(preferencelist=("plain",)) else ""]).lower()
                if all((w[5:] in str(parsed.get("From", "")).lower()) if w.startswith("from:")
                       else (w in text) for w in words):
                    hits.append(message_id)
            # Like Gmail: by RECEIVE time (here: the order the inbox was filled), newest
            # first — never by the sender-controlled Date header.
            limit = int((params or {}).get("maxResults") or 10)
            return {"messages": [{"id": mid} for mid in list(reversed(hits))[:limit]]}
        if method == "GET" and path.startswith("/messages/"):
            message_id = path.rsplit("/", 1)[1]
            raw = self.inbox.get(message_id)
            if (params or {}).get("format") == "raw":
                return None if raw is None else {"id": message_id, "raw": b64url(raw)}
            return None if raw is None else self._full(message_id, raw)
        if method == "GET" and path == "/profile":
            return {"emailAddress": OWN}
        raise AssertionError(f"unexpected Gmail call: {method} {path}")

    # -- test helpers ---------------------------------------------------------
    def replace_draft(self, draft_id: str, raw: bytes) -> None:
        """Someone else (a second client) changes the draft at Gmail."""
        self.drafts[draft_id]["raw"] = raw


def sent_message(raw: bytes):
    return email.message_from_bytes(raw, policy=email_policy.default)


async def approval_chain(folder: str):
    """The REAL approval chain with a synthetic device (see the note suites)."""
    import mobile_attest_helper as H
    from solvio.security.approval import ApprovalBroker
    from solvio.security.mobile_approval import bridge as B
    from solvio.security.mobile_approval import control as C
    from solvio.security.mobile_approval import identity
    from solvio.security.mobile_approval import store as SA

    ordner = tempfile.mkdtemp(dir=folder)
    speicher = SA.ApprovalControlStore(os.path.join(ordner, "approval.sqlite3"))
    await speicher.open()
    cp = C.MobileApprovalControlPlane(
        speicher, identity.MacSigningKey.load_or_create(ordner),
        identity.load_or_create_core_instance_id(ordner),
        attest_verifier=H.fake_verifier(),
        app_id="WQ8CG7R53R.de.solvio.approvals",
        allowed_environments={"development"})
    freigeber = B.MobileApprover()
    co = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=freigeber), freigeber)
    geraet = await H.enroll_attested(cp)
    return cp, co, geraet, H, speicher


async def sign(cp, co, geraet, H, approval_id: str, counter: list[int]) -> None:
    """The owner's Face ID decision on the synthetic device."""
    from solvio.security.mobile_approval import protocol as PR
    draht, grund = await cp.issue_challenge(approval_id=approval_id,
                                            device_id=geraet.device_id)
    if draht is None:
        raise AssertionError(f"challenge refused: {grund}")
    counter[0] += 1
    _res, status = await co.apply_mobile_decision(
        **H.sign_decision(geraet, PR.b64d(draht["payload_b64"]), counter=counter[0]))
    if status != "ok":
        raise AssertionError(f"approval: {status}")
