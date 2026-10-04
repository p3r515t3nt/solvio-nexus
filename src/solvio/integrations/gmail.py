"""Gmail ueber die offizielle REST-API — und eine Sanierung vor dem Modell.

Derselbe Zuschnitt wie beim Kalender: JSON ueber HTTPS mit dem `aiohttp`, das
SOLVIO ohnehin hat, kein SDK, keine neue Abhaengigkeit. Dieselbe OAuth-Anmeldung,
derselbe Desktop-Client — nur zwei zusaetzliche Scopes.

**Alles, was von hier kommt, ist fremder Text.** Absender, Betreff, Koerper,
Signaturen, zitierte Antworten, Anhangsnamen: geschrieben von Menschen, die nicht
der Besitzer sind. Diese Datei liefert deshalb zwei Dinge — einen Zugang und eine
**Sanierung**, die den Text entschaerft, bevor er ueberhaupt in die Naehe eines
Modells kommt.

Die Sanierung macht E-Mail nicht vertrauenswuerdig. Sie entfernt nur die Tricks,
mit denen Text sich vor dem menschlichen Auge versteckt oder seine Richtung
verdreht — unsichtbare Zeichen, Bidi-Steuerung, weissgestellte Absaetze,
HTML-Kommentare. Die eigentliche Grenze bleibt der `TrustContext`.
"""
from __future__ import annotations

import base64
import html as _html
import re
import contextlib
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage as _MimeMessage
from typing import Any

import aiohttp

from solvio.logging_setup import get_logger

log = get_logger("gmail")

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Die Herkunft des Token-Endpunkts — das ZIEL, an das der Tresor bindet.
_TOKEN_ORIGIN = "https://oauth2.googleapis.com"
_REFRESH_MARGIN = 120.0

#: Obergrenze fuer Text, der aus einer Mail in einen Modellkontext geht. Eine Mail
#: kann beliebig lang sein; ein Kontextfenster nicht, und ein Angreifer wuerde
#: genau das ausnutzen.
MAX_BODY_CHARS = 8000

#: Unsichtbare Zeichen: Breitenlos, Wortverbinder, BOM. Beliebt, um einem
#: Menschen etwas anderes zu zeigen als dem Modell.
_INVISIBLE = re.compile(r"[​-‏⁠-⁤﻿­]")

#: Bidi-Steuerzeichen. Sie koennen die Leserichtung drehen, sodass eine Zeile
#: harmlos aussieht und anders gemeint ist.
_BIDI = re.compile(r"[‪-‮⁦-⁩]")

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
#: Absaetze, die im Browser unsichtbar sind. Was der Mensch nicht sieht, darf auch
#: das Modell nicht als Inhalt lesen.
_HIDDEN = re.compile(
    r"<([a-z][a-z0-9]*)\b[^>]*style\s*=\s*[\"'][^\"']*"
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0)"
    r"[^\"']*[\"'][^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE)
_BREAK = re.compile(r"<(br|/p|/div|/tr|/li)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def sanitize_text(value: str, *, limit: int = MAX_BODY_CHARS) -> str:
    """Entschaerft Klartext: unsichtbare Zeichen weg, Laenge begrenzt."""
    cleaned = _BIDI.sub("", _INVISIBLE.sub("", value or ""))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _BLANKS.sub("\n\n", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n[… gekuerzt]"
    return cleaned


def html_to_text(value: str, *, limit: int = MAX_BODY_CHARS) -> str:
    """Macht aus HTML sichtbaren Text — und nur aus dem Sichtbaren.

    Reihenfolge ist wichtig: erst Verstecktes entfernen, dann Tags. Umgekehrt
    bliebe der Inhalt eines `display:none`-Absatzes als nackter Text stehen.
    """
    text = value or ""
    text = _COMMENT.sub(" ", text)
    text = _SCRIPT_STYLE.sub(" ", text)
    for _ in range(3):        # verschachtelte versteckte Bloecke
        text, count = _HIDDEN.subn(" ", text)
        if not count:
            break
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return sanitize_text(text, limit=limit)


def _decode_header(value: str) -> str:
    try:
        return sanitize_text(str(make_header(decode_header(value or ""))), limit=400)
    except Exception:  # noqa: BLE001 - ein kaputter Header darf nichts kippen
        return sanitize_text(value or "", limit=400)


def _b64url(data: str) -> bytes:
    padded = (data or "") + "=" * (-len(data or "") % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class EmailMessage:
    """Eine Nachricht, wie SOLVIO sie sieht — durchweg fremder Text."""
    message_id: str
    thread_id: str
    sender: str = ""
    to: str = ""
    cc: str = ""
    subject: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    attachment_names: tuple[str, ...] = field(default_factory=tuple)
    attachments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: Die RFC-Kennung (`Message-ID`), nicht Gmails Objektkennung — die braucht eine
    #: Antwort fuer `In-Reply-To`, damit sie auch beim Empfaenger im Verlauf steht.
    rfc_message_id: str = ""
    received_at_ms: int = 0

    @property
    def unread(self) -> bool:
        return "UNREAD" in self.labels

    def as_data(self, *, with_body: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.message_id, "thread_id": self.thread_id,
            "from": self.sender, "subject": self.subject, "date": self.date,
            "unread": self.unread,
            "sent": "SENT" in self.labels, "draft": "DRAFT" in self.labels,
            "received_at_ms": self.received_at_ms,
            # Die Marke reist mit: was hier steht, ist Information, kein Auftrag.
            "content_trust": "untrusted_email",
        }
        if self.snippet and not with_body:
            out["snippet"] = self.snippet
        if with_body:
            out["to"] = self.to
            if self.cc:
                out["cc"] = self.cc
            out["body"] = self.body
            if self.attachment_names:
                out["attachments"] = list(self.attachment_names)
        return out


def _walk_parts(part: Any, acc: dict[str, list[Any]]) -> None:
    if not isinstance(part, dict):
        return
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename:
        clean_name = _decode_header(filename)
        acc.setdefault("names", []).append(clean_name)
        attachment_id = body.get("attachmentId")
        if attachment_id:
            acc.setdefault("attachments", []).append({
                "filename": clean_name,
                "mime_type": mime,
                "attachment_id": str(attachment_id),
                "size": _nonnegative_int(body.get("size")),
            })
        # A named part is a file boundary. Gmail also expands .eml files into
        # child parts; their text/headers/files belong to that attachment only.
        # Missing attachment bytes remain unreadable, never replaced by a child.
        return
    data = body.get("data")
    if data:
        try:
            raw = _b64url(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        if mime == "text/plain":
            acc.setdefault("plain", []).append(raw)
        elif mime == "text/html":
            acc.setdefault("html", []).append(raw)
    for child in (part.get("parts") if isinstance(part.get("parts"), list) else []):
        _walk_parts(child, acc)


def message_from_api(payload: dict[str, Any]) -> EmailMessage:
    """Normalisiert Gmails Rohform — Klartext bevorzugt, HTML als Rueckfall."""
    payload = payload if isinstance(payload, dict) else {}
    envelope = payload.get("payload")
    envelope = envelope if isinstance(envelope, dict) else {}
    # Wehrhaft gegen eine kaputte Antwort: liefert der Anbieter statt der
    # Kopfzeilen etwas anderes, darf daraus KEIN `AttributeError` werden, der als
    # rohe Ausnahme bis zum Modell durchschlaegt. Eine Mail ist Fremdmaterial —
    # ihre Struktur genauso wie ihr Text.
    raw_headers = envelope.get("headers")
    headers = {(h.get("name") or "").lower(): h.get("value", "")
               for h in (raw_headers if isinstance(raw_headers, list) else [])
               if isinstance(h, dict)}
    acc: dict[str, list[Any]] = {}
    _walk_parts(envelope, acc)
    if acc.get("plain"):
        body = sanitize_text("\n".join(acc["plain"]))
    elif acc.get("html"):
        body = html_to_text("\n".join(acc["html"]))
    else:
        body = ""
    return EmailMessage(
        message_id=payload.get("id", ""), thread_id=payload.get("threadId", ""),
        sender=_decode_header(headers.get("from", "")),
        to=_decode_header(headers.get("to", "")),
        cc=_decode_header(headers.get("cc", "")),
        subject=_decode_header(headers.get("subject", "")),
        date=_decode_header(headers.get("date", "")),
        received_at_ms=(int(str(payload.get("internalDate")))
                        if str(payload.get("internalDate", "")).isdigit() else 0),
        snippet=sanitize_text(_html.unescape(payload.get("snippet", "")), limit=400),
        body=body,
        labels=tuple(payload.get("labelIds") or []),
        attachment_names=tuple(acc.get("names") or []),
        attachments=tuple(acc.get("attachments") or []),
        rfc_message_id=_decode_header(headers.get("message-id", "")))


class GmailAuthError(Exception):
    """Der Anbieter hat die Anmeldung abgelehnt. Enthaelt nie ein Geheimnis."""


class GmailMessageTooLarge(ValueError):
    """Die unveraenderte Originalmail passt nicht in den begrenzten Anhang."""


class SearchResults(list):
    """A bounded search page with explicit completeness, never inferred from length."""
    def __init__(self, rows=(), *, complete=False):
        super().__init__(rows)
        self.complete = complete


class Gmail:
    """Der Zugang. Nur die fuenf Operationen, die V1 braucht."""

    #: Dieselbe Google-Anmeldung wie der Kalender — deshalb dieselben Verweise.
    #: Zwei Eintraege fuer dasselbe Geheimnis waeren zwei Dinge, die getrennt
    #: veralten.
    CLIENT_SECRET_REF = "secret://google/oauth-client"
    REFRESH_TOKEN_REF = "secret://google/refresh"

    def __init__(self, *, client_id: str, client_secret: str = "",
                 refresh_token: str = "", timeout: float = 15.0,
                 broker: Any = None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._access_token = ""
        self._expires_at = 0.0
        self._token_credential_versions = None
        self._broker = broker

    @property
    def uses_vault(self) -> bool:
        return self._broker is not None

    def permits(self, capability: str) -> bool:
        """Nennt der Tresor diese Faehigkeit fuer BEIDE Google-Eintraege? Nur Metadaten,
        nie ein Wert — damit eine Faehigkeit, die der Tresor ohnehin abweisen wuerde,
        niemanden erst um Face ID bittet."""
        if not self.uses_vault:
            return True
        for ref in (self.CLIENT_SECRET_REF, self.REFRESH_TOKEN_REF):
            described = self._broker.describe(ref)
            if (described is None or described.get("status") != "active"
                    or capability not in described.get("allowed_capabilities", ())):
                return False
        return True

    @contextlib.contextmanager
    def _credentials(self):
        """Die beiden dauerhaften Geheimnisse — nur fuer die Dauer der Erneuerung.

        Der `use()`-Aufruf steht ABSICHTLICH hier und nicht in einem Helfer: der
        Tresor prueft den Modulnamen des Aufrufers gegen den behaupteten
        Executor, und ein gemeinsamer Helfer in einem anderen Modul wuerde genau
        diese Bindung aufheben.
        """
        if not self.uses_vault:
            if not (self._client_secret and self._refresh_token):
                raise GmailAuthError("credentials_missing")
            yield self._client_secret, self._refresh_token
            return
        from solvio.secret_vault.policy import ExecutorId
        with self._broker.use(self.CLIENT_SECRET_REF, executor=ExecutorId.HTTP,
                              target=_TOKEN_ORIGIN) as secret:
            with self._broker.use(self.REFRESH_TOKEN_REF, executor=ExecutorId.HTTP,
                                  target=_TOKEN_ORIGIN) as refresh:
                yield secret.plaintext(), refresh.plaintext()

    def _authorise_cached(self) -> None:
        """Der Tresor entscheidet AUCH ueber ein bereits geholtes Token.

        DEBT-0193, live gemessen: ohne diese Pruefung war ein warmes
        Zugangstoken stille Autoritaet. Eine Faehigkeit ausserhalb des Scopes
        lief durch, weil eine ANDERE, erlaubte Faehigkeit kurz zuvor auf
        derselben gemeinsamen Instanz ein Token geholt hatte. Der Riegel des
        Tresors griff nie, weil er nie gefragt wurde.

        `authorize()` steht ABSICHTLICH hier und in keinem gemeinsamen Helfer
        eines anderen Moduls: der Tresor nimmt den Modulnamen des Aufrufers per
        Rahmen-Inspektion, und ein Helfer anderswo wuerde genau diese Bindung
        aufheben — derselbe Grund wie bei `_credentials()`.

        Beide Verweise werden geprueft. Ein entzogener Scope auf nur einem von
        beiden ist ein entzogener Scope.
        """
        if not self.uses_vault:
            return
        from solvio.secret_vault.policy import ExecutorId
        for ref in (self.CLIENT_SECRET_REF, self.REFRESH_TOKEN_REF):
            self._broker.authorize(ref, executor=ExecutorId.HTTP,
                                   target=_TOKEN_ORIGIN)

    def _credential_versions(self):
        """Safe metadata binding of a derived token; no secret value or hash.

        Rotating either durable credential invalidates the old access token,
        even while its provider expiry has not arrived. This is independent
        of the permission check, which still runs for every cache hit.
        """
        if not self.uses_vault:
            return (self._client_id, ())
        versions = []
        effective_client_id = self._client_id
        for ref in (self.CLIENT_SECRET_REF, self.REFRESH_TOKEN_REF):
            metadata = self._broker.describe(ref)
            if metadata is None or type(metadata.get("version")) is not int or metadata["version"] < 1:
                raise GmailAuthError("credentials_missing")
            if ref == self.CLIENT_SECRET_REF and "oauth_client_id" in metadata:
                candidate = metadata["oauth_client_id"]
                if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com", candidate):
                    raise GmailAuthError("credentials_invalid")
                effective_client_id = candidate
            versions.append((ref, metadata["version"], metadata.get("rotated_at", "")))
        return (effective_client_id, tuple(versions))

    async def _token(self) -> str:
        self._authorise_cached()
        versions = self._credential_versions()
        if (self._access_token and time.monotonic() < self._expires_at - _REFRESH_MARGIN
                and self._token_credential_versions == versions):
            return self._access_token
        self._access_token = ""
        self._expires_at = 0.0
        self._token_credential_versions = None
        with self._credentials() as (secret, refresh):
            data = {"client_id": versions[0], "client_secret": secret,
                    "refresh_token": refresh, "grant_type": "refresh_token"}
        if self._credential_versions() != versions:
            raise GmailAuthError("credentials_changed")
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(_TOKEN_URL, data=data) as reply:
                    body = await reply.json(content_type=None)
                    if reply.status != 200 or not body.get("access_token"):
                        reason = str(body.get("error", reply.status))
                        log.error("gmail.auth_failed", reason=reason)
                        raise GmailAuthError(reason)
                    self._access_token = body["access_token"]
                    self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            self._authorise_cached()
            if self._credential_versions() != versions:
                raise GmailAuthError("credentials_changed")
        except BaseException:
            self._access_token = ""
            self._expires_at = 0.0
            raise
        self._token_credential_versions = versions
        return self._access_token

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None) -> Any:
        versions = self._credential_versions()
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            token = await self._token()
            if self._credential_versions() != versions:
                raise GmailAuthError("credentials_changed")
            headers = {"Authorization": f"Bearer {token}"}
            async with session.request(method, _API + path, headers=headers,
                                       params=params, json=json_body) as reply:
                if reply.status == 401:
                    self._access_token = ""
                    raise GmailAuthError("unauthorized")
                if reply.status in (404, 410):
                    return None
                if reply.status >= 400:
                    log.error("gmail.api_error", status=reply.status)
                    raise RuntimeError(f"gmail api {reply.status}")
                return await reply.json(content_type=None)

    # -- Lesen ---------------------------------------------------------------
    async def search(self, query: str = "", *, limit: int = 10,
                     label: str = "") -> list[EmailMessage]:
        """Sucht und laedt NUR die Treffer — nie das ganze Postfach.

        Gmails Liste liefert bloss Kennungen; die Nachrichten werden einzeln und
        bewusst begrenzt nachgeladen. Weniger Daten verlassen den Mac, und ein
        Modellkontext bleibt ueberschaubar.
        """
        params: dict[str, Any] = {"maxResults": str(max(1, min(int(limit), 25)))}
        if query:
            params["q"] = query
        if label:
            params["labelIds"] = label
        listing = await self._request("GET", "/messages", params=params)
        out = SearchResults(complete=isinstance(listing, dict) and not listing.get('nextPageToken'))
        for entry in (listing or {}).get("messages") or []:
            payload = await self._request("GET", f"/messages/{entry['id']}",
                                          params={"format": "full"})
            if payload:
                out.append(message_from_api(payload))
                if payload.get('id') != entry['id']:
                    out.complete = False
            else:
                out.complete = False
        return out

    async def message(self, message_id: str) -> EmailMessage | None:
        payload = await self._request("GET", f"/messages/{message_id}",
                                      params={"format": "full"})
        return message_from_api(payload) if payload else None

    async def raw_message(self, message_id: str, *, max_bytes: int) -> bytes | None:
        """Original-RFC-Mail, nur fuer den gebundenen Weiterleitungsanhang.

        Diese Bytes gehen nie als Lese-/Werkzeugantwort an ein Modell. Keine
        Rekonstruktion aus dem sanierten Lesetext; Gmail liefert das Original.
        """
        payload = await self._request("GET", f"/messages/{message_id}",
                                      params={"format": "raw"})
        if not isinstance(payload, dict) or payload.get("id") != message_id:
            return None
        raw = payload.get("raw")
        if not isinstance(raw, str) or not raw:
            return None
        if len(raw) > 4 * ((max_bytes + 2) // 3):
            raise GmailMessageTooLarge()
        try:
            data = base64.b64decode(raw + "=" * (-len(raw) % 4),
                                    altchars=b"-_", validate=True)
        except (ValueError, UnicodeError):
            return None
        if len(data) > max_bytes:
            raise GmailMessageTooLarge()
        return data or None

    async def attachment(self, message_id: str, attachment_id: str) -> bytes | None:
        """Holt genau einen Gmail-Anhang und dekodiert Gmails base64url-Rumpf."""
        payload = await self._request(
            "GET", f"/messages/{message_id}/attachments/{attachment_id}")
        if payload is None:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, str):
            return None
        try:
            return _b64url(data)
        except (ValueError, TypeError, UnicodeError):
            return None

    async def thread(self, thread_id: str) -> list[EmailMessage]:
        payload = await self._request("GET", f"/threads/{thread_id}",
                                      params={"format": "full"})
        return [message_from_api(m) for m in (payload or {}).get("messages") or []]

    async def profile(self) -> dict[str, Any]:
        return await self._request("GET", "/profile") or {}

    # -- Schreiben -----------------------------------------------------------
    @staticmethod
    def _mime(*, to: str, subject: str, body: str, sender: str = "",
              in_reply_to: str = "", references: str = "",
              attachments: tuple = ()) -> str:
        message = _MimeMessage()
        message["To"] = to
        message["Subject"] = subject
        if sender:
            message["From"] = sender
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = references or in_reply_to
        message.set_content(body)
        # Nur fuer eine Weiterleitung: die Anhaenge der Originalmail, Byte fuer Byte.
        # SOLVIO erzeugt selbst keine Anhaenge; was hier steht, ist gezaehlt und
        # steht mit Pruefsumme im Freigabetext (ADR-0041).
        for name, mime_type, data in attachments:
            maintype, _, subtype = str(mime_type or "").partition("/")
            if not maintype or not subtype:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(bytes(data), maintype=maintype, subtype=subtype,
                                   filename=name)
        return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    async def create_draft(self, *, to: str, subject: str, body: str,
                           thread_id: str = "", in_reply_to: str = "",
                           attachments: tuple = ()) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": {
            "raw": self._mime(to=to, subject=subject, body=body,
                              in_reply_to=in_reply_to, attachments=attachments)}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        return await self._request("POST", "/drafts", json_body=payload) or {}

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        return await self._request("GET", f"/drafts/{draft_id}",
                                   params={"format": "full"})

    async def send_draft(self, draft_id: str, *, raw: str = "",
                         thread_id: str = "") -> dict[str, Any]:
        """Versendet einen bestehenden Entwurf. Genau ein Aussenwirkungspunkt.

        Mit `raw` die dokumentierte Form „aktualisieren und senden": hinaus gehen
        genau diese Bytes, nicht das, was zwischen Lesen und Senden bei Gmail liegt
        (https://developers.google.com/workspace/gmail/api/guides/drafts#send-drafts).
        """
        body: dict[str, Any] = {"id": draft_id}
        if raw:
            body["message"] = {"raw": raw}
            if thread_id:
                body["message"]["threadId"] = thread_id
        return await self._request("POST", "/drafts/send", json_body=body) or {}

    async def delete_draft(self, draft_id: str) -> None:
        await self._request("DELETE", f"/drafts/{draft_id}")

    async def sent_with_subject(self, subject: str, *, limit: int = 5) -> list[EmailMessage]:
        """Fuer die Nachpruefung: was liegt wirklich in „Gesendet"?"""
        return await self.search(f'subject:"{subject}"', limit=limit, label="SENT")


def from_settings(settings: Any, *, broker: Any = None) -> Gmail | None:
    """Baut den Zugang aus derselben Google-Anmeldung wie der Kalender.

    Der Tresor hat Vorrang: liegen die beiden Geheimnisse dort, wird der
    Rueckfall aus der Konfiguration gar nicht erst gelesen.
    """
    client_id = (getattr(settings, "google_calendar_client_id", "") or "").strip()
    if not client_id:
        return None
    if broker is not None and all(broker.exists(ref) for ref in
                                  (Gmail.CLIENT_SECRET_REF, Gmail.REFRESH_TOKEN_REF)):
        return Gmail(client_id=client_id, broker=broker)
    secret = (getattr(settings, "google_calendar_client_secret", "") or "").strip()
    refresh = (getattr(settings, "google_calendar_refresh_token", "") or "").strip()
    if not (secret and refresh):
        return None
    return Gmail(client_id=client_id, client_secret=secret, refresh_token=refresh)
