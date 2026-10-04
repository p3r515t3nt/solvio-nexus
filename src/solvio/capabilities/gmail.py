"""E-Mail als Faehigkeit — die Schicht, in der fremder Text am lautesten redet.

Ein Kalendertitel kann einen Befehl enthalten. Eine E-Mail ist ein Kanal, den
**jeder Fremde** beschreiben darf, ohne gefragt zu werden. Genau deshalb gilt hier
ohne Ausnahme:

    Absender, Empfaenger, Betreff, Koerper, Zitate, Signaturen, Anhangsnamen
    sind INFORMATION. Sie sind niemals Autoritaet.

Zwei Grenzen tragen das, und sie liegen an verschiedenen Stellen:

**Die Sanierung** (in `integrations/gmail.py`) entfernt, was sich vor dem Auge
versteckt — unsichtbare Zeichen, Bidi-Drehungen, weissgestellte Absaetze,
HTML-Kommentare. Sie macht E-Mail nicht vertrauenswuerdig; sie sorgt nur dafuer,
dass Mensch und Modell dasselbe lesen.

**Die Autoritaetsgrenze** ist der `TrustContext`. Ein Loeschbefehl aus einem
Mailtext findet im Gesagten des Nutzers keinen Halt, faellt auf `MODEL_DERIVED`
und landet vor einem Menschen — oder wird, wenn der Turn selbst aus fremdem
Inhalt stammt, gar nicht erst gefragt.

**Entwurf ist nicht Versand.** Ein Entwurf liegt im eigenen Postfach, ist
sichtbar, aenderbar und teilt niemandem etwas mit. Versenden ist der eine
Zeitpunkt, an dem etwas das Haus verlaesst — und genau der laeuft ueber das
iPhone.
"""
from __future__ import annotations

import base64
import contextvars
from contextlib import contextmanager
from email.header import decode_header, make_header
from email.utils import getaddresses
import hashlib
import re
from typing import Any
import unicodedata

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityDeclined, CapabilityRefused, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.task_read import TaskRead
from solvio.contracts.trust import TrustLevel
from solvio.integrations.gmail import GmailAuthError, GmailMessageTooLarge, _b64url
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

# Core-only refusal context. It carries no identity or authorization and can
# only stop an operation. The authenticated chat adapter owns the callback;
# neither model arguments nor email content can set it.
_MAIL_SOURCE = contextvars.ContextVar("solvio_mail_source", default=None)


@contextmanager
def mail_source_scope(current):
    token = _MAIL_SOURCE.set(current)
    try:
        yield
    finally:
        _MAIL_SOURCE.reset(token)


async def _require_mail_source():
    current = _MAIL_SOURCE.get()
    if current is None:
        return
    from solvio.security.mobile_approval.execution import SafeExecutionFailure
    try:
        valid = await current()
    except Exception:  # a broken check is not evidence of a live source
        valid = False
    if valid is not True:
        raise SafeExecutionFailure("mail_source_ended")


log = get_logger("gmail")

#: Mailinhalt traegt immer diese Klasse — auch nach der Sanierung.
CONTENT_TRUST = TrustLevel.UNTRUSTED_EMAIL

#: Was in einer Freigabe angezeigt werden kann, ohne dass der Nutzer scrollen
#: muesste, bis er aufgibt. Lange Weiterleitungen nutzen nach Owner-Entscheidung
#: vom 28.09.2026 die gebundene Originaldatei; neue Begleittexte bleiben begrenzt.
MAX_SENDABLE_BODY = 4000

#: Weiterleitung (Stufe S2, ADR-0041): die Anhaenge der Originalmail gehen mit —
#: gezaehlt, begrenzt und mit Pruefsumme im Freigabetext.
MAX_FORWARD_ATTACHMENTS = 10
MAX_FORWARD_BYTES = 10_000_000

_ADDRESS = re.compile(r"[^@<>\s,;]+@[^@<>\s,;]+\.[A-Za-z]{2,}")


def extract_address(value: str) -> str:
    """Zieht die reine Adresse aus `Max Muster <max@example.com>`."""
    found = _ADDRESS.search(value or "")
    return found.group(0) if found else ""


_WHEN = {"type": "string"}

SPECS: dict[str, CapabilitySpec] = {
    "gmail_list_recent": CapabilitySpec(
        name="gmail_list_recent", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "only_unread": {"type": "boolean"}, "limit": {"type": "integer"}}},
        description="Nennt die neuesten E-Mails im Posteingang."),
    "gmail_search": CapabilitySpec(
        name="gmail_search", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"]},
        description="Sucht E-Mails nach Stichwort, Absender oder Betreff."),
    "gmail_read_message": CapabilitySpec(
        name="gmail_read_message", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "message_id": {"type": "string"}}, "required": ["message_id"]},
        description="Liest eine einzelne E-Mail vollstaendig."),
    "gmail_read_thread": CapabilitySpec(
        name="gmail_read_thread", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "thread_id": {"type": "string"}}, "required": ["thread_id"]},
        description="Liest einen ganzen Gespraechsverlauf."),
    # Ein Entwurf teilt niemandem etwas mit und liegt sichtbar im eigenen
    # Postfach. Deshalb HARMLESS — das Risiko steigt trotzdem, sobald das Modell
    # den Empfaenger erfunden hat, und dann greift die Freigabe.
    "gmail_create_draft": CapabilitySpec(
        name="gmail_create_draft", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}, "reply_to_message": {"type": "string"},
            "forward_message": {"type": "string"}},
            "required": ["body"]},
        description="Legt einen E-Mail-Entwurf an — auch eine Weiterleitung mit den "
                    "Anhaengen der Originalmail. Versendet nichts."),
    # Der eine Punkt, an dem etwas das Haus verlaesst.
    "gmail_send_draft": CapabilitySpec(
        name="gmail_send_draft", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "draft_id": {"type": "string"}, "to": {"type": "string"},
            "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["draft_id"]},
        description="Versendet einen zuvor angelegten Entwurf. Der Freigabetext zeigt den "
                    "tatsaechlichen Entwurf: Empfaenger, Betreff, Text und Anhaenge."),
}


def _message_of(stored: dict[str, Any]):
    from solvio.integrations.gmail import message_from_api
    return message_from_api((stored or {}).get("message") or {})


def attachment_line(name: str, data: bytes) -> str:
    """Ein Anhang, wie er im Freigabetext steht: Name, genaue Groesse, Pruefsumme.

    Die Pruefsumme ist gekuerzt, damit die Zeile lesbar bleibt; gebunden sind
    zusammen mit Name und Bytezahl trotzdem genau diese Bytes.
    """
    return f"{name} · {len(data)} Bytes · SHA-256 {hashlib.sha256(data).hexdigest()[:16]}"


def _raw_header(value: str) -> str:
    """Eine Kopfzeile dekodiert, aber UNGESAEUBERT — um zu sehen, was die Anzeige wegliesse."""
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001 - kaputte Kodierung ist dann eben der Rohtext
        return value or ""


def _hidden(text: str) -> bool:
    """Steht etwas darin, das kein Mensch auf dem Bildschirm saehe?

    Formatzeichen (breitenlos, Bidi-Drehungen) und Steuerzeichen ausser Zeilenende
    und Tabulator. Die Anzeige saeubert sie weg — also darf ein solcher Entwurf gar
    nicht erst zur Freigabe.
    """
    return any(unicodedata.category(ch) in ("Cf", "Co", "Cs") or ch in _BLANK_LOOKING
               or (unicodedata.category(ch) == "Cc" and ch not in "\n\r\t")
               for ch in text or "")


#: Zeichen, die als Buchstabe oder Symbol gelten, aber leer dargestellt werden
#: (Hangul-Fueller, Braille-Leerzeichen, mongolischer Vokaltrenner). Mit ihnen liesse
#: sich Text im scheinbar Leeren kodieren (Review S2, Befund 4).
#: Eine oder mehrere RFC-Nachrichtenkennungen, wie `In-Reply-To` sie tragen darf.
_MESSAGE_IDS = re.compile(r"<[^<>\s]{1,250}>(?:\s+<[^<>\s]{1,250}>)*")

_BLANK_LOOKING = frozenset("\u115f\u1160\u3164\uffa0\u2800\u180e")


def _draft_parts(payload: dict[str, Any]) -> str | None:
    """Der eine Klartext eines Entwurfs — wenn er genau aus diesem und benannten Anhaengen
    besteht. Ein HTML-Teil, ein unbenannter Datenteil oder ein zweiter Text standen in
    keinem Freigabetext; dann `None`."""
    texts: list[str] = []

    def walk(part: Any) -> bool:
        if not isinstance(part, dict):
            return False
        # Gmail can expose parsed children even for an opaque .eml attachment.
        # Its attachmentId addresses the entire file, which is fetched and
        # bound separately. Children must not become outer text or attachments.
        body = part.get("body") or {}
        if part.get("filename"):
            return bool(body.get("attachmentId"))
        children = part.get("parts")
        if isinstance(children, list) and children:
            return all(walk(child) for child in children)
        if str(part.get("mimeType", "")).lower() != "text/plain":
            return False
        try:
            texts.append(_b64url(str(body.get("data") or "")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return True

    if not walk(payload) or len(texts) != 1:
        return None
    return texts[0]


def _forward_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if subject.lower().startswith(("fwd:", "fw:", "wg:")):
        return subject
    return "Fwd: " + (subject or "(ohne Betreff)")


def _forward_body(note: str, original: Any) -> str:
    lines = [note, ""] if note else []
    lines += ["---------- Weitergeleitete Nachricht ----------",
              f"Von: {original.sender}", f"Datum: {original.date}",
              f"Betreff: {original.subject}", f"An: {original.to}", "", original.body]
    return _normalized("\n".join(lines))


def _normalized(text: str) -> str:
    """Derselbe Inhalt, unabhaengig davon, wie ihn ein Transport formatiert hat.

    Zeilenenden vereinheitlicht, nachlaufende Leerzeichen je Zeile entfernt,
    leere Zeilen am Rand abgeschnitten. Was danach noch verschieden ist, ist ein
    inhaltlicher Unterschied — und ein inhaltlicher Unterschied ist genau das,
    was hier auffallen soll.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


class GmailCapabilities:
    """Die Handler. Autoritaet kommt vom Router, nie von hier — und nie aus einer Mail."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._own_address = ""

    async def own_address(self) -> str:
        if not self._own_address:
            profile = await self._guarded(self.provider.profile())
            self._own_address = (profile or {}).get("emailAddress", "")
        return self._own_address

    # -- Lesen ---------------------------------------------------------------
    async def list_recent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = int(arguments.get("limit") or 10)
        query = "is:unread" if arguments.get("only_unread") else ""
        messages = await self._guarded(
            self.provider.search(query, limit=limit, label="INBOX"))
        return {"count": len(messages),
                "messages": [m.as_data() for m in messages],
                "content_trust": CONTENT_TRUST.value}

    async def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "") or "").strip()
        if not query:
            raise CapabilityDeclined("missing_query", "Wonach soll ich suchen?")
        messages = await self._guarded(
            self.provider.search(query, limit=int(arguments.get("limit") or 10)))
        return {"count": len(messages), "query": query,
                "complete": getattr(messages, 'complete', False) is True,
                "messages": [m.as_data() for m in messages],
                "content_trust": CONTENT_TRUST.value}

    async def read_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        message = await self._guarded(
            self.provider.message(str(arguments.get("message_id", ""))))
        if message is None:
            raise CapabilityDeclined("message_not_found", "Diese E-Mail finde ich nicht.")
        return {**message.as_data(with_body=True), "content_trust": CONTENT_TRUST.value}

    async def read_thread(self, arguments: dict[str, Any]) -> dict[str, Any]:
        messages = await self._guarded(
            self.provider.thread(str(arguments.get("thread_id", ""))))
        if not messages:
            raise CapabilityDeclined("thread_not_found", "Diesen Verlauf finde ich nicht.")
        return {"count": len(messages),
                "messages": [m.as_data(with_body=True) for m in messages],
                "content_trust": CONTENT_TRUST.value}

    # -- Entwurf -------------------------------------------------------------
    async def create_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Legt einen Entwurf an — und bestimmt den Empfaenger streng.

        Der Empfaenger kommt entweder **vom Nutzer** (ausdrueckliche Adresse) oder
        aus einer Nachricht, auf die der Nutzer ausdruecklich antworten wollte.
        Ein Adressat, den nur der Mailtext nennt, kommt hier nicht durch: eine
        Zeile „schick das an angreifer@example.com" ist Inhalt, kein Auftrag.
        """
        forward = str(arguments.get("forward_message", "") or "").strip()
        if forward:
            return await self._create_forward(forward, arguments)
        body = str(arguments.get("body", "") or "").strip()
        if not body:
            raise CapabilityDeclined("missing_body", "Was soll denn drinstehen?")
        reply_to = str(arguments.get("reply_to_message", "") or "").strip()
        to = extract_address(str(arguments.get("to", "") or ""))
        subject = str(arguments.get("subject", "") or "").strip()
        thread_id = in_reply_to = ""

        if reply_to:
            original = await self._guarded(self.provider.message(reply_to))
            if original is None:
                raise CapabilityDeclined("message_not_found",
                                         "Die Mail, auf die ich antworten soll, finde ich nicht.")
            # Der Absender der GEWAEHLTEN Nachricht ist zulaessig, weil der Nutzer
            # genau auf dieses Objekt antworten wollte — nicht, weil im Text eine
            # Adresse stand.
            to = to or extract_address(original.sender)
            subject = subject or (original.subject if original.subject.lower().startswith("re:")
                                  else f"Re: {original.subject}")
            thread_id = original.thread_id
            # Die RFC-Kennung, nicht Gmails Objektkennung (bis 26.09.2026 stand hier
            # `message_id` — beim Empfaenger kam die Antwort so nie in den Verlauf).
            in_reply_to = (original.rfc_message_id
                           if _MESSAGE_IDS.fullmatch(original.rfc_message_id or "") else "")
        if not to:
            raise CapabilityDeclined(
                "missing_recipient",
                "An wen soll die Mail gehen? Sag mir die Adresse oder auf welche "
                "Nachricht ich antworten soll.")
        if not subject:
            subject = "(ohne Betreff)"
        await _require_mail_source()
        draft = await self._guarded(self.provider.create_draft(
            to=to, subject=subject, body=body, thread_id=thread_id,
            in_reply_to=in_reply_to))
        draft_id = (draft or {}).get("id", "")
        if not draft_id:
            raise ExecutorUnavailable("gmail did not return a draft id")
        return {"action": "draft_created", "draft_id": draft_id, "to": to,
                "subject": subject, "body": body, "sent": False,
                "hint": "Der Entwurf liegt in deinem Postfach. Verschickt ist nichts. "
                        "Zum Versenden brauche ich deine Freigabe per Face ID."}

    async def _create_forward(self, message_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Eine Weiterleitung als Entwurf: die gewaehlte Mail samt ihrer Anhaenge.

        Welche Mail, bestimmt der Nutzer (eine Kennung aus einem Suchergebnis); an
        wen, ebenfalls — eine Adresse aus dem Mailtext ist Inhalt, kein Auftrag, und
        der Versand zeigt den Empfaenger ohnehin vor Face ID. Anhaenge, die sich
        nicht lesen lassen, machen die Weiterleitung unvollstaendig — dann keine.
        """
        if str(arguments.get("reply_to_message", "") or "").strip():
            raise CapabilityDeclined("forward_and_reply",
                                     "Weiterleiten oder antworten — beides zugleich geht nicht.")
        to = extract_address(str(arguments.get("to", "") or ""))
        if not to:
            raise CapabilityDeclined("missing_recipient",
                                     "An welche Adresse soll ich die Mail weiterleiten?")
        original = await self._guarded(self.provider.message(message_id))
        if original is None:
            raise CapabilityDeclined("message_not_found",
                                     "Die Mail, die ich weiterleiten soll, finde ich nicht.")
        note = str(arguments.get("body", "") or "").strip()
        subject = str(arguments.get("subject", "") or "").strip() or _forward_subject(original.subject)
        body = _forward_body(note, original)
        if len(body) > MAX_SENDABLE_BODY:
            return await self._create_original_forward(original, to, subject, note)
        if len(original.attachments) != len(original.attachment_names):
            raise CapabilityRefused(
                "forward_attachment_unreadable",
                "Nicht jeden Anhang dieser Mail kann ich lesen — so leite ich sie nicht "
                "weiter, sonst fehlte etwas.")
        if len(original.attachments) > MAX_FORWARD_ATTACHMENTS:
            raise CapabilityRefused("forward_too_many_attachments",
                                    "Diese Mail hat zu viele Anhaenge zum Weiterleiten.")
        files: list[tuple[str, str, bytes]] = []
        total = 0
        for item in original.attachments:
            data = await self._guarded(
                self.provider.attachment(original.message_id, item["attachment_id"]))
            if data is None:
                raise ExecutorUnavailable("gmail attachment unreadable")
            total += len(data)
            if total > MAX_FORWARD_BYTES:
                raise CapabilityRefused("forward_too_large",
                                        "Die Anhaenge sind zusammen zu gross zum Weiterleiten.")
            files.append((item["filename"], item.get("mime_type", ""), data))
        await _require_mail_source()
        draft = await self._guarded(self.provider.create_draft(
            to=to, subject=subject, body=body, attachments=tuple(files)))
        draft_id = (draft or {}).get("id", "")
        if not draft_id:
            raise ExecutorUnavailable("gmail did not return a draft id")
        return {"action": "draft_created", "draft_id": draft_id, "to": to,
                "subject": subject, "body": body, "forwarded_message": original.message_id,
                "attachments": [attachment_line(name, data) for name, _mime, data in files],
                "sent": False,
                "hint": "Der Weiterleitungsentwurf liegt in deinem Postfach. Verschickt ist "
                        "nichts. Zum Versenden brauche ich deine Freigabe per Face ID."}

    async def _create_original_forward(self, original, to: str, subject: str,
                                       note: str) -> dict[str, Any]:
        # Existing draft/attachment/Face-ID path; no new authority or send path.
        # Octet-stream keeps the .eml an opaque, byte-exact file. Nested RFC822
        # MIME parsing must not promote its HTML/headers to the outer message.
        body = _normalized((note + "\n\n" if note else "") +
            "Die vollstaendige Originalmail ist als Datei Originalmail.eml angehaengt, "
            "einschliesslich ihrer Kopfzeilen und Anhaenge. Ihr Inhalt wird in der "
            "Freigabe nicht als Text angezeigt.")
        if len(body) > MAX_SENDABLE_BODY:
            raise CapabilityRefused("forward_note_too_long",
                "Die Originalmail kann als Datei mitgehen. Der Begleittext ist zu lang "
                "fuer die vollstaendige Freigabeanzeige. Bitte kuerze den Begleittext.")
        try:
            raw = await self._guarded(self.provider.raw_message(
                original.message_id, max_bytes=MAX_FORWARD_BYTES))
        except GmailMessageTooLarge:
            raise CapabilityRefused("forward_too_large",
                "Die Originalmail ist groesser als die erlaubten 10 MB. "
                "Bitte waehle einen kleineren Ausschnitt oder einzelne Anhaenge.") from None
        if raw is None:
            raise CapabilityRefused("forward_original_unreadable",
                "Die vollstaendige Originaldatei ist gerade nicht abrufbar. "
                "Ich habe keinen Entwurf angelegt und nichts verschickt.")
        files = (("Originalmail.eml", "application/octet-stream", raw),)
        await _require_mail_source()
        draft = await self._guarded(self.provider.create_draft(
            to=to, subject=subject, body=body, attachments=files))
        draft_id = (draft or {}).get("id", "")
        if not draft_id:
            raise ExecutorUnavailable("gmail did not return a draft id")
        return {"action": "draft_created", "draft_id": draft_id, "to": to,
                "subject": subject, "body": body, "forwarded_message": original.message_id,
                "attachments": [attachment_line("Originalmail.eml", raw)], "sent": False,
                "forward_format": "original_file",
                "hint": "Die lange Mail liegt vollstaendig als Originalmail-Anhang im "
                        "Entwurf. Vor dem Versand brauchst du nur diese konkrete "
                        "Weiterleitung per Face ID freizugeben. Verschickt ist nichts."}

    # -- Versand -------------------------------------------------------------
    async def describe_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Was auf dem iPhone steht und gebunden wird: der TATSAECHLICHE Entwurf.

        Empfaenger, Betreff, voller Text und jeder Anhang (Name, Bytes, Pruefsumme)
        kommen aus dem Entwurf bei Gmail, nicht aus dem Modell. Der Router bildet
        diese Beschreibung vor der Freigabe und direkt vor dem Versand erneut; hat
        sich der Entwurf dazwischen geaendert, passt der Digest nicht mehr (ADR-0041).
        Nennt das Modell Empfaenger, Betreff oder Text, muessen sie passen.
        """
        draft_id = str(arguments.get("draft_id", "") or "").strip()
        if not draft_id:
            raise CapabilityDeclined("incomplete_send", "Welchen Entwurf soll ich senden?")
        described, _files, _thread = await self._draft_description(draft_id)
        if (str(arguments.get("to", "") or "").strip()
                and extract_address(str(arguments.get("to") or "")) != described["to"]):
            raise CapabilityRefused("draft_recipient_mismatch",
                                    "Der Entwurf geht an jemand anderen als genannt.")
        if "subject" in arguments and str(arguments.get("subject") or "") != described["subject"]:
            raise CapabilityRefused("draft_subject_mismatch", "Der Betreff des Entwurfs weicht ab.")
        if "body" in arguments and _normalized(str(arguments.get("body") or "")) != described["body"]:
            raise CapabilityRefused("draft_body_mismatch",
                                    "Der Text des Entwurfs ist ein anderer als genannt.")
        return described

    async def _draft_description(self, draft_id: str) -> tuple[dict[str, Any], list, str]:
        """Beschreibung UND die geprueften Anhangsbytes eines Entwurfs — oder eine Absage.

        Abgesagt wird alles, was der Bildschirm nicht vollstaendig zeigen koennte:
        mehr oder andere Empfaenger (auch Cc, Bcc, Resent-*), andere Teile als genau
        ein Klartext plus benannte Anhaenge, unsichtbare Zeichen oder ein gekuerzter
        Betreff, ein zu langer Text, ein Anhang ohne pruefbare Bytes.
        """
        stored = await self._guarded(self.provider.get_draft(draft_id))
        if stored is None:
            raise CapabilityDeclined("draft_not_found", "Diesen Entwurf finde ich nicht.")
        message = _message_of(stored)
        payload = ((stored.get("message") or {}).get("payload") or {}) if isinstance(stored, dict) else {}
        headers = [(str(h.get("name", "")).lower(), str(h.get("value", "") or ""))
                   for h in (payload.get("headers") or []) if isinstance(h, dict)]
        to_values = [_raw_header(value) for name, value in headers if name == "to"]
        recipients = [address for _name, address in getaddresses(to_values) if address]
        others = [value for name, value in headers
                  if name in ("cc", "bcc", "resent-to", "resent-cc", "resent-bcc") and value.strip()]
        shown_to = extract_address(message.to)
        if len(recipients) != 1 or others or recipients[0] != shown_to:
            raise CapabilityRefused("draft_recipients_changed",
                                    "Der Entwurf hat nicht genau einen Empfaenger — ich sende "
                                    "ihn nicht.")
        plain = _draft_parts(payload)
        subject_raw = "".join(_raw_header(value) for name, value in headers if name == "subject")
        if (plain is None or _hidden(plain) or _hidden(subject_raw)
                or "\n" in subject_raw or "\r" in subject_raw
                or subject_raw.strip() != message.subject):
            raise CapabilityRefused("draft_not_showable",
                                    "Der Entwurf enthaelt etwas, das ich dir nicht vollstaendig "
                                    "zeigen kann — ich sende ihn nicht.")
        body = _normalized(message.body)
        if len(body) > MAX_SENDABLE_BODY:
            raise CapabilityRefused(
                "body_too_long",
                "Der Text ist zu lang, um ihn dir vollstaendig zur Freigabe zu zeigen. "
                "Ich sende nichts, was du nicht ganz gesehen hast.")
        if len(message.attachments) != len(message.attachment_names):
            raise CapabilityRefused("draft_attachment_unreadable",
                                    "Einen Anhang des Entwurfs kann ich nicht pruefen — ich "
                                    "sende ihn nicht.")
        lines, files = [], []
        for item in message.attachments:
            data = await self._guarded(
                self.provider.attachment(message.message_id, item["attachment_id"]))
            if data is None:
                raise ExecutorUnavailable("gmail draft attachment unreadable")
            lines.append(attachment_line(item["filename"], data))
            files.append((item["filename"], item.get("mime_type", ""), data))
        reply = " ".join(_raw_header(value) for name, value in headers
                         if name == "in-reply-to").strip()
        if reply and (_hidden(reply) or not _MESSAGE_IDS.fullmatch(reply)):
            # Nur Nachrichtenkennungen — freier Text oder ein Zeilenumbruch stuende sonst
            # als scheinbar eigene Zeile auf dem Bildschirm (Review S2, dritte Runde).
            raise CapabilityRefused("draft_not_showable",
                                    "Der Entwurf enthaelt etwas, das ich dir nicht vollstaendig "
                                    "zeigen kann — ich sende ihn nicht.")
        described: dict[str, Any] = {"draft_id": draft_id, "to": shown_to,
                                     "subject": message.subject, "body": body}
        if reply:
            # Eine Antwort bleibt in ihrem Verlauf (Review S2, Befund 3) — und der
            # Bildschirm sagt, worauf geantwortet wird.
            described["in_reply_to"] = reply
        if lines:
            described["attachments"] = lines
        thread = str(((stored.get("message") or {}).get("threadId")) or "")
        return described, files, thread

    async def send_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Versendet genau das, was der Mensch freigegeben hat — Byte fuer Byte.

        Die Mail wird aus der freigegebenen Beschreibung gebaut (Empfaenger,
        Betreff, Text) plus den Anhangsbytes, deren Pruefsummen dort stehen, und
        mit Gmails „aktualisieren und senden" verschickt. Was sonst bei Gmail im
        Entwurf liegt, verlaesst das Haus nicht. Vorher wird der Entwurf noch einmal
        gelesen: weicht er ab, geht nichts hinaus. Ausserhalb einer freigegebenen
        Ausfuehrung wird nie gesendet (ADR-0041).
        """
        from solvio.capabilities.router import approved_description
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        draft_id = str(arguments.get("draft_id", "") or "").strip()
        approved = approved_description()
        if approved is None or approved.get("draft_id") != draft_id:
            raise SafeExecutionFailure("send_not_approved")
        try:
            current, files, thread = await self._draft_description(draft_id)
        except (CapabilityDeclined, CapabilityRefused, ExecutorUnavailable) as exc:
            raise SafeExecutionFailure(getattr(exc, "reason", type(exc).__name__)) from None
        if current != approved:
            raise SafeExecutionFailure("draft_changed")
        try:
            raw = self.provider._mime(to=approved["to"], subject=approved["subject"],
                                      body=approved["body"], attachments=tuple(files),
                                      in_reply_to=approved.get("in_reply_to", ""))
        except (ValueError, TypeError):
            # Die Mail laesst sich nicht bauen — dann ging sicher nichts hinaus.
            raise SafeExecutionFailure("mail_not_buildable") from None
        await _require_mail_source()
        sent = await self._guarded(self.provider.send_draft(draft_id, raw=raw, thread_id=thread))
        message_id = (sent or {}).get("id", "")
        return {"action": "sent", "to": approved["to"], "subject": approved["subject"],
                "attachments": len(files), "message_id": message_id, "sent": bool(message_id)}

    def permits(self, capability: str) -> bool:
        """Ob der Tresor diese Faehigkeit fuer den Google-Zugang nennt (nur Metadaten)."""
        permits = getattr(self.provider, "permits", None)
        return True if permits is None else bool(permits(capability))

    async def send_composed(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        """Fuer `communication_send`: eine Nachricht, deren Inhalt deren eigene Freigabe
        schon bindet. Hinaus gehen genau diese Bytes (aktualisieren und senden)."""
        await _require_mail_source()
        draft = await self._guarded(self.provider.create_draft(to=to, subject=subject, body=body))
        draft_id = (draft or {}).get("id", "")
        if not draft_id:
            raise ExecutorUnavailable("gmail did not return a draft id")
        raw = self.provider._mime(to=to, subject=subject, body=body)
        await _require_mail_source()
        sent = await self._guarded(self.provider.send_draft(draft_id, raw=raw))
        message_id = (sent or {}).get("id", "")
        return {"action": "sent", "to": to, "subject": subject,
                "message_id": message_id, "sent": bool(message_id)}

    @staticmethod
    def _draft_fields(stored: dict[str, Any]) -> dict[str, str]:
        message = _message_of(stored)
        return {"to": message.to, "subject": message.subject, "body": message.body}

    # -- Werkzeug ------------------------------------------------------------
    @staticmethod
    async def _guarded(awaitable):
        try:
            return await awaitable
        except (CapabilityDeclined, CapabilityRefused, GmailMessageTooLarge):
            raise
        except TimeoutError:
            # Abgeschickt, keine Antwort. Bei einer E-Mail ist das der teuerste
            # unklare Ausgang ueberhaupt — nichts wird automatisch wiederholt.
            raise AmbiguousExecution("gmail did not answer in time") from None
        except GmailAuthError as exc:
            raise ExecutorUnavailable(f"gmail authorization: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ExecutorUnavailable(f"gmail failed: {type(exc).__name__}") from exc


class GmailTaskRead(TaskRead):
    """Die vier lesenden Mailfaehigkeiten als Auftragswerkzeuge (Stufe S1)."""

    ROUTE = ("google.gmail", "read")
    OWNER = GmailCapabilities
    METHODS = {
        "gmail_list_recent": GmailCapabilities.list_recent,
        "gmail_search": GmailCapabilities.search,
        "gmail_read_message": GmailCapabilities.read_message,
        "gmail_read_thread": GmailCapabilities.read_thread,
    }
    SPECS = SPECS


def register(router: CapabilityRouter, capabilities: GmailCapabilities) -> list[str]:
    handlers = {
        "gmail_list_recent": capabilities.list_recent,
        "gmail_search": capabilities.search,
        "gmail_read_message": capabilities.read_message,
        "gmail_read_thread": capabilities.read_thread,
        "gmail_create_draft": capabilities.create_draft,
        "gmail_send_draft": capabilities.send_draft,
    }
    describers = {"gmail_send_draft": capabilities.describe_send}
    for name, handler in handlers.items():
        if name in GmailTaskRead.METHODS:
            task_read = GmailTaskRead(name, handler)
            try:
                task_read._binding()
            except ValueError:
                pass  # Test-/Altadapter behalten ihren Handler, nie einen Kostenvertrag.
            else:
                handler = task_read
        router.register(SPECS[name], handler, describe=describers.get(name))
    return sorted(handlers)
