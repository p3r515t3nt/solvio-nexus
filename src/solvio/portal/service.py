"""Der Portal-Arbeiter. Laeuft unter einer eigenen Unix-Kennung, nicht im Core.

Was hier passiert, passiert absichtlich woanders. Ein angemeldeter Browser haelt
Sitzungskekse, gefuellte Felder und — fuer Augenblicke — ein Passwort im
Speicher. Solange das im selben Prozess wie der Core laege, waere jede
Ausbruchsluecke im Browser zugleich eine im Gedaechtnis, im Gespraechsspeicher
und in der Freigabedatenbank. Ein eigener Prozess unter einer eigenen Kennung
macht daraus zwei Fragen statt einer.

Der Arbeiter ist bewusst dumm. Er entscheidet nichts:

* Er kennt den Tresor nicht. Ein Geheimnis bekommt er einzeln, im Augenblick der
  freigegebenen Verwendung, und er behaelt es nicht.
* Er kennt keine Freigabe. Er bekommt ein Manifest, das bereits freigegeben ist,
  und prueft nur noch, ob die Seite noch dazu passt.
* Er kennt kein Modell. Es gibt keinen Weg, ihm freien Text als Anweisung zu
  geben; das Nachrichtenformat kennt nur die aufgezaehlten Operationen.

Und er glaubt seinem Anrufer nicht aufs Wort: beim Annehmen der Verbindung
fragt er den Kern, wer da verbindet, und redet nur mit dem Core.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import shutil
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from solvio.browser.cdp import BrowserProcess
from solvio.browser.page import BrowserPage
from solvio.browser.policy import check as policy_check
from solvio.logging_setup import get_logger
from solvio.portal import protocol as P
from solvio.portal.binding import PortalBinding
from solvio.portal.build import installed_build
from solvio.portal.manifest import ActionManifest, FieldBinding, drifted, page_signature
from solvio.portal.permit import PermitBook, origin_of
from solvio.portal.redact import SecretRedactor

log = get_logger("portal")

#: Wie lange eine angemeldete Sitzung ohne Zutun bestehen darf. Danach ist sie
#: fort, samt Profil. Eine Sitzung, die niemand mehr benutzt, ist nur noch ein
#: offenes Fenster.
SESSION_IDLE = 20 * 60.0

#: Wie lange eine Sitzung hoechstens lebt, auch bei Benutzung.
SESSION_MAX = 60 * 60.0

#: Was nach dem Absenden auf Antwort gewartet wird.
SUBMIT_SETTLE = 3.0

#: Fest verdrahtete Seitenskripte des Portalwegs. Wie im oeffentlichen Browser:
#: das Modell erreicht sie nicht, eingesetzt werden nur Zeichenketten aus der
#: konfigurierten Bindung — nie aus einem Modellargument.
_FILL = """
(function (selector, value) {
  const el = document.querySelector(selector);
  if (!el) return {ok: false, reason: 'field_not_found'};
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(
      el.constructor.prototype, 'value')?.set;
  if (setter) { setter.call(el, value); } else { el.value = value; }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true};
})(%SELECTOR%, %VALUE%)
"""

_INSPECT_FORM = """
(function (formSelector, submitSelector) {
  const form = formSelector ? document.querySelector(formSelector) : null;
  const submit = submitSelector ? document.querySelector(submitSelector) : null;
  const names = [];
  if (form) for (const el of form.elements) if (el.name) names.push(el.name);
  return {
    found: !!form || !!submit,
    origin: location.origin,
    url: location.href,
    action: form ? (form.getAttribute('action') || '') : '',
    method: form ? ((form.getAttribute('method') || 'get').toUpperCase()) : 'GET',
    fields: names,
    has_submit: !!submit
  };
})(%FORMSEL%, %SUBMITSEL%)
"""

_CLICK = """
(function (selector) {
  const el = document.querySelector(selector);
  if (!el) return {ok: false, reason: 'submit_not_found'};
  el.click();
  return {ok: true};
})(%SELECTOR%)
"""

_VISIBLE_TEXT = """
(function (limit) {
  const t = (document.body ? document.body.innerText : '') || '';
  return {url: location.href, title: document.title || '',
          text: t.replace(/[ \\t]+/g, ' ').slice(0, limit),
          truncated: t.length > limit};
})(%LIMIT%)
"""

#: Struktur statt Fliesstext. Eine ganze Portalseite ans Modell zu geben waere
#: das Gegenteil von Datensparsamkeit — und die meisten Zeichen darin sind
#: Navigation, Fusszeile und Zustimmungsbanner. Was hier herauskommt, sind
#: Ueberschriften, beschriftete Zahlen und ausgewiesene Hinweise.
_SUMMARIZE = """
(function (limit) {
  function vis(el) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    if (parseFloat(s.fontSize || '16') < 1) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const r = el.getBoundingClientRect();
    return !(r.width === 0 && r.height === 0);
  }
  function txt(el) {
    return ((el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 200);
  }
  const out = {url: location.href, title: document.title || '',
               headings: [], metrics: [], alerts: [], sections: [], account: []};
  for (const el of document.querySelectorAll(
      '[class*=account],[class*=user],[class*=profile],[class*=workspace],' +
      '[data-testid*=user],[data-testid*=account],[aria-label*=Konto]')) {
    if (out.account.length >= 8) break;
    if (!vis(el)) continue;
    const t = txt(el); if (t && t.length <= 160 && out.account.indexOf(t) < 0) out.account.push(t);
  }
  for (const el of document.querySelectorAll('h1,h2,h3,[role=heading]')) {
    if (out.headings.length >= limit) break;
    if (!vis(el)) continue;
    const t = txt(el); if (t) out.headings.push(t);
  }
  for (const el of document.querySelectorAll('[role=alert],[role=status],[aria-live]')) {
    if (out.alerts.length >= limit) break;
    if (!vis(el)) continue;
    const t = txt(el); if (t) out.alerts.push(t);
  }
  // Beschriftete Zahlen: das uebliche Muster ist ein kleiner Beschriftungstext
  // neben einer grossen Zahl. Gesucht wird deshalb nach Elementen, deren
  // sichtbarer Text mit einer Zahl beginnt, und deren Nachbarschaft eine
  // Beschriftung hergibt.
  const seen = new Set();
  for (const el of document.querySelectorAll('dl>div,li,article,section,[class*=stat],[class*=metric],[class*=kpi],[class*=card],[data-testid]')) {
    if (out.metrics.length >= limit) break;
    if (!vis(el)) continue;
    const t = txt(el);
    if (!t || t.length > 120 || seen.has(t)) continue;
    if (!/\d/.test(t)) continue;
    if (el.querySelector('h1,h2,h3,section,article')) continue;
    seen.add(t); out.metrics.push(t);
  }
  for (const el of document.querySelectorAll('nav a,[role=navigation] a')) {
    if (out.sections.length >= limit) break;
    if (!vis(el)) continue;
    const t = txt(el); if (t && out.sections.indexOf(t) < 0) out.sections.push(t);
  }
  return out;
})(%LIMIT%)
"""

MAX_TEXT = 12000

#: Wie viele Eintraege je Art hoechstens zurueckkommen.
MAX_ITEMS = 40
MAX_SESSIONS = 50

#: Der Baum, aus dem dieser Prozess wirklich geladen hat — beim Import
#: aufgeloest und danach unveraenderlich.
#:
#: Die Aufloesung ist der Punkt. Die Auslieferung haengt einen Symlink um; wer
#: den Bauzustand spaeter ueber den Symlink rechnet, meldet den NEUEN Code,
#: waehrend im Speicher noch der ALTE laeuft. Das waere die genaue Umkehrung
#: dessen, was die Pruefung leisten soll.
LOADED_FROM = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))

#: Pfade, die der Arbeiter NICHT erreichen darf. Er meldet auf Anfrage, ob er sie
#: lesen kann — nur ja oder nein, nie einen Inhalt.
#:
#: Das ist ausdruecklich kein Dateizugriff durch die Hintertuer: die Liste ist
#: eine Konstante, die Antwort ist ein Wahrheitswert, und es gibt keinen Weg,
#: einen anderen Pfad hineinzureichen. Der Grund fuer die Auskunft ist, dass die
#: Trennung sonst nur behauptet waere — der Arbeiter ist der einzige Prozess, der
#: sie von innen messen kann.
SEALED = (
    "~/solvio-core/.env",
    "~/.solvio-approvals/approval_control.sqlite3",
    "~/.solvio-approvals/core_signing_key.pem",
    # Der produktive Pfad heisst anders als der Modulstandard (DEBT-0112).
    # Beide stehen hier, damit die Zusicherung den Ort trifft, der wirklich
    # benutzt wird.
    "~/.solvio-approvals-production/approval_control.sqlite3",
    "~/.solvio-approvals-production/core_signing_key.pem",
    "~/.solvio-vault/vault.sqlite3",
    "~/.solvio-vault/recovery.json",
    "~/.solvio/conversations.sqlite3",
    "~/.solvio/memory",
    "~/.solvio-portal/vault.bin",
    "~/.ssh",
    "~/Library/Application Support/Google/Chrome",
    "~/solvio-core/src/solvio/security",
)


def _reachable(path: str) -> bool:
    """Kommt dieser Prozess an den Pfad heran? Nur das, kein Inhalt."""
    resolved = os.path.expanduser(path.replace("~", "/Users/solvio", 1)
                                  if path.startswith("~") else path)
    try:
        if os.path.isdir(resolved):
            os.listdir(resolved)
        else:
            with open(resolved, "rb") as handle:
                handle.read(1)
    except OSError:
        return False
    return True


def _js_string(value: str) -> str:
    """Eine Zeichenkette sicher in ein Skript einsetzen.

    `json.dumps` kodiert Anfuehrungszeichen, Zeilenenden und alles Weitere so,
    dass daraus kein Ausdruck werden kann. Eingesetzt werden ohnehin nur Werte
    aus der konfigurierten Bindung und Geheimnisse aus dem Tresor — nie Text,
    den ein Modell oder eine Seite geliefert hat.
    """
    return json.dumps(value, ensure_ascii=False)


class PortalSession:
    """Ein angemeldeter Browser, sein Profil und seine Frist."""

    def __init__(self, session_id: str, binding: PortalBinding,
                 process: BrowserProcess, page: BrowserPage,
                 permits: PermitBook, redactor: SecretRedactor, *, owner_principal: str = "") -> None:
        self.session_id = session_id
        self.binding = binding
        self.process = process
        self.page = page
        self.permits = permits
        self.redactor = redactor
        self.opened = time.monotonic()
        self.touched = self.opened
        self.authenticated = False
        # Core router identity, never page content. Empty legacy sessions are
        # deliberately not adopted by the owner catalogue.
        self.owner_principal = owner_principal

    def touch(self) -> None:
        self.touched = time.monotonic()

    def expired(self, *, now: float | None = None) -> str:
        moment = now if now is not None else time.monotonic()
        if moment - self.touched > SESSION_IDLE:
            return "idle"
        if moment - self.opened > SESSION_MAX:
            return "max_lifetime"
        return ""

    async def destroy(self) -> None:
        """Alles fort: Erlaubnisse, Geheimnisse, Browser, Profil."""
        self.permits.clear()
        self.redactor.clear()
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001
            pass
        profile = self.process.profile_dir
        try:
            await self.process.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(profile, ignore_errors=True)
        log.info("portal.session_destroyed", session_id=self.session_id,
                 profile_gone=not os.path.exists(profile))


class PortalWorker:
    """Der Dienst. Nimmt Auftraege vom Core an — und nur von ihm."""

    def __init__(self, *, socket_path: str, core_uid: int,
                 allow_private_targets: bool = False) -> None:
        self.socket_path = socket_path
        self.core_uid = core_uid
        # Nur fuer Tests gegen eine oertliche Attrappe. Der produktive Aufbau
        # setzt das nie — belegt durch einen Test, der genau das behauptet.
        self.allow_private_targets = allow_private_targets
        self.sessions: dict[str, PortalSession] = {}
        self._server: socket.socket | None = None
        self._sequence = 0
        # Only native opening reservations. The durable authority and unknown
        # boundary live in Core; a worker restart never recreates a session.
        self._openings: dict[str, dict] = {}

    # -- Bedienung -----------------------------------------------------------
    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        operation = str(message.get("op", ""))
        if operation not in P.OPERATIONS:
            return P.failure("unknown_operation", operation[:40])
        await self._reap()
        try:
            handler = getattr(self, "_op_" + operation)
            return await handler(message)
        except KeyError:
            return P.failure("unknown_session")
        except Exception as exc:  # noqa: BLE001 - ein Auftrag reisst den Dienst nie mit
            log.error("portal.operation_failed", op=operation, kind=type(exc).__name__)
            return P.failure("worker_failed", type(exc).__name__)

    def _build(self) -> str:
        """Der Bauzustand der Dateien, aus denen dieser Prozess laeuft.

        Gerechnet, nicht abgelesen: eine Datei mit der Antwort koennte man
        mitfaelschen, die Hashes der geladenen Module nicht. Und gerechnet ueber
        `LOADED_FROM`, nicht ueber den Symlink — sonst meldete ein laufender
        Prozess nach einer Auslieferung fremden Code als seinen eigenen.
        """
        return installed_build(LOADED_FROM)

    async def _op_ping(self, _message: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "uid": os.getuid(), "gid": os.getgid(),
                "build": self._build(),
                "version": P.PROTOCOL_VERSION, "sessions": len(self.sessions),
                "home": os.path.expanduser("~"),
                # Die Selbstauskunft ueber die Grenze. Wahrheitswerte, keine Inhalte.
                "reachable": {path: _reachable(path) for path in SEALED}}

    async def _op_open_session(self, message: dict[str, Any]) -> dict[str, Any]:
        binding = PortalBinding.from_data(message["binding"])
        owner = message.get("owner_principal", "")
        if type(owner) is not str or len(owner) > 128 or any(ord(c) < 32 for c in owner):
            return P.failure("invalid_owner")
        reference = message.get("connection_ref", "")
        digest = hashlib.sha256(json.dumps(binding.as_data(), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if reference:
            if (not owner or type(reference) is not str or not re.fullmatch(r"pc-[0-9a-f]{32}", reference)):
                return P.failure("invalid_connection_ref")
            previous = self._openings.get(reference)
            if previous:
                if previous['owner_principal'] != owner or previous['binding_digest'] != digest:
                    return P.failure("connection_binding_changed")
                return await self._op_open_status({'owner_principal': owner, 'connection_ref': reference})
            if len(self._openings) >= 128:
                return P.failure("connection_capacity_reached")
        self._sequence += 1
        session_id = f"ps-{os.getpid():d}-{self._sequence:d}"
        if reference:
            self._openings[reference] = {'state': 'opening', 'session_id': session_id,
                'owner_principal': owner, 'binding_digest': digest}
        process = BrowserProcess()
        try:
            await process.start()
            cdp = await process.new_page_socket()
            permits = PermitBook()
            page = BrowserPage(cdp, write_gate=lambda *, url, method: permits.allow(url=url, method=method),
                               origin_gate=binding.allows)
            await page.prepare()
        except BaseException:
            if reference:
                self._openings[reference]['state'] = 'unknown'
            try:
                await process.stop()
                shutil.rmtree(process.profile_dir, ignore_errors=True)
            except Exception:
                pass
            raise
        session = PortalSession(session_id, binding, process, page, permits,
                                SecretRedactor(), owner_principal=owner)
        self.sessions[session_id] = session
        if reference:
            self._openings[reference]['state'] = 'opened'
        log.info("portal.session_opened", session_id=session_id,
                 portal=binding.portal_id, uid=os.getuid())
        return {"ok": True, "session_id": session_id, "uid": os.getuid()}

    async def _op_open_status(self, message: dict[str, Any]) -> dict[str, Any]:
        entry = self._openings.get(message.get('connection_ref'))
        if not entry or not message.get('owner_principal') or entry['owner_principal'] != message['owner_principal']:
            return P.failure('unknown_connection')
        state = entry['state']
        if state == 'opened' and entry['session_id'] not in self.sessions:
            state = 'closed'
        return {'ok': state == 'opened', **entry, 'state': state, 'build': self._build()}

    async def _op_navigate(self, message: dict[str, Any]) -> dict[str, Any]:
        session = self._session(message)
        url = str(message.get("url", ""))
        if not session.binding.allows(url):
            return P.failure("origin_not_bound", origin_of(url))
        verdict = self._policy(url)
        if not verdict:
            return P.failure("private_network_blocked", origin_of(url))
        try:
            await session.page.navigate(url)
        except Exception as exc:  # noqa: BLE001
            return P.failure("navigation_failed", type(exc).__name__)
        session.touch()
        return {"ok": True, **await self._read(session)}

    async def _op_read(self, message: dict[str, Any]) -> dict[str, Any]:
        session = self._session(message)
        if ((session.owner_principal or "owner_principal" in message)
                and (not message.get("owner_principal")
                     or message["owner_principal"] != session.owner_principal)):
            return P.failure("unknown_session")
        session.touch()
        payload = {"ok": True, **await self._read(session)}
        if message.get("structured"):
            data = await self._evaluate(session, _SUMMARIZE.replace(
                "%LIMIT%", str(MAX_ITEMS)))
            if isinstance(data, dict):
                payload["structure"] = session.redactor.scrub(
                    {k: v for k, v in data.items() if k != "url"})
        payload["session_binding"] = self._session_binding(session, payload)
        payload["authenticated"] = payload["session_binding"]["authenticated"]
        return payload

    def _session_binding(self, session: PortalSession, payload: dict) -> dict:
        """Same native origin/path/marker proof for READ and the catalogue."""
        success_path = session.binding.success_path.rstrip("/")
        actual_path = urlsplit(payload.get("url", "")).path
        # A login URL's redirect query/fragment is not the authenticated page.
        # Only the configured path itself or its child path can prove success.
        path_matches = (not success_path or actual_path == success_path
                        or actual_path.startswith(success_path + "/"))
        return {
            "session_id": session.session_id,
            "portal_id": session.binding.portal_id,
            "owner_principal": session.owner_principal,
            "binding_digest": hashlib.sha256(json.dumps(session.binding.as_data(),
                sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
            "authenticated": bool(not session.expired() and session.authenticated and path_matches
                and session.binding.allows(payload.get("url", ""))
                and session.binding.authenticated_by(url=payload.get("url", ""), text=payload.get("text", ""))),
        }

    async def _op_list_sessions(self, message: dict[str, Any]) -> dict[str, Any]:
        owner, limit = message.get("owner_principal"), message.get("limit", MAX_SESSIONS)
        if (set(message) - {"op", "owner_principal", "limit"} or type(owner) is not str
                or not owner or len(owner) > 128 or any(ord(c) < 32 for c in owner)
                or type(limit) is not int or not 1 <= limit <= MAX_SESSIONS):
            return P.failure("invalid_session_catalogue")
        candidates = [s for _, s in sorted(self.sessions.items())
                      if s.owner_principal == owner and not s.expired()]
        items = []
        for session in candidates[:limit]:
            try:
                payload = await self._read(session)
            except Exception:
                payload = {}
            # A close/expiry during CDP reading cannot return a usable session.
            if self.sessions.get(session.session_id) is not session or session.expired():
                continue
            native = self._session_binding(session, payload)
            items.append({**native, "expires_in_s": max(0, int(min(
                SESSION_IDLE - (time.monotonic() - session.touched),
                SESSION_MAX - (time.monotonic() - session.opened))))})
        return {"ok": True, "build": self._build(), "observed_at": time.time(),
                "items": items, "truncated": len(candidates) > limit}

    async def _op_probe_action(self, message: dict[str, Any]) -> dict[str, Any]:
        """Beschreibt die Seite so, wie sie JETZT ist — fuer das Manifest.

        Es wird nichts gefuellt und nichts geklickt. Genau das ist der Punkt aus
        §9: ein Wert, der vor der Freigabe im Feld steht, ist bereits verraten.
        """
        session = self._session(message)
        binding = session.binding
        script = (_INSPECT_FORM
                  .replace("%FORMSEL%", _js_string(binding.form_selector))
                  .replace("%SUBMITSEL%", _js_string(binding.submit_selector)))
        info = await self._evaluate(session, script)
        if not isinstance(info, dict) or not info.get("found"):
            return P.failure("form_not_found")
        signature = page_signature(origin=str(info.get("origin", "")),
                                   form_action=str(info.get("action", "")),
                                   method=str(info.get("method", "GET")),
                                   field_names=info.get("fields") or [],
                                   target=binding.form_selector or binding.submit_selector)
        session.touch()
        return {"ok": True, "origin": info.get("origin", ""),
                "url": info.get("url", ""), "action": info.get("action", ""),
                "method": info.get("method", "GET"),
                "fields": info.get("fields") or [],
                "page_signature": signature,
                "session_binding": self._session_binding(session, {'url': info.get('url', ''), 'text': ''})}

    async def _op_execute(self, message: dict[str, Any]) -> dict[str, Any]:
        """Fuehrt genau die freigegebene Aktion aus — wenn die Seite noch passt."""
        session = self._session(message)
        binding = session.binding
        manifest = _manifest_from(message["manifest"])
        secrets_in = message.get("secrets") or {}

        blocked = binding.may_inject(manifest.page_url) if manifest.credential_alias else ""
        if blocked:
            return P.failure(blocked, manifest.origin)

        # Erst noch einmal hinsehen. Zwischen Freigabe und Ausfuehrung liegen
        # Sekunden, in denen fremdes JavaScript das Ziel tauschen kann.
        live = await self._op_probe_action({"session_id": session.session_id,
            **({"owner_principal": session.owner_principal} if session.owner_principal else {})})
        if not live.get("ok"):
            return P.failure("approval_drift", "page_gone")
        drift = drifted(manifest, {"origin": live["origin"], "method": live["method"],
                                   "target": manifest.target,
                                   "page_signature": live["page_signature"]})
        if drift:
            log.warning("portal.approval_drift", session_id=session.session_id,
                        reason=drift)
            return P.failure("approval_drift", drift)

        # Jetzt erst fuellen. Ein Geheimnis lebt ab hier Sekunden im Speicher
        # dieses Prozesses und nirgends sonst.
        for entry in manifest.fields:
            value = secrets_in.get(entry.alias) if entry.alias else entry.value
            if entry.alias:
                if not value:
                    return P.failure("missing_secret", entry.alias)
                session.redactor.remember(value)
            filled = await self._evaluate(session, _FILL
                                          .replace("%SELECTOR%", _js_string(entry.selector))
                                          .replace("%VALUE%", _js_string(value or "")))
            if not isinstance(filled, dict) or not filled.get("ok"):
                return P.failure("field_not_found", entry.selector)

        permit = None
        if manifest.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            # An die Schreib-Herkunft der BINDUNG, nicht an die der Seite. Bei
            # dieser Anwendung liegt das Formular auf der einen und der POST geht
            # an die andere — wer das verwechselt, sperrt die eigene Anmeldung aus.
            permit = session.permits.issue(
                action_id=str(message.get("action_id", manifest.digest()[:16])),
                origin=binding.write_origin, method=manifest.method)

        clicked = await self._evaluate(session, _CLICK.replace(
            "%SELECTOR%", _js_string(binding.submit_selector)))
        if not isinstance(clicked, dict) or not clicked.get("ok"):
            if permit is not None:
                session.permits.revoke(permit.action_id)
            return P.failure("submit_not_found", binding.submit_selector)

        await asyncio.sleep(SUBMIT_SETTLE)
        used = permit is not None and permit.spent
        # Die Erlaubnis verschwindet in jedem Fall. Eine ungenutzte Erlaubnis,
        # die liegenbleibt, waere ein offenes Fenster ohne Anlass.
        if permit is not None:
            session.permits.revoke(permit.action_id)

        after = await self._read(session)
        session.touch()
        # Was unterwegs abgewiesen wurde. Ohne diese Auskunft ist eine
        # gescheiterte Anmeldung stumm, und man sucht an der falschen Stelle.
        blocked = sorted(set(session.page.state.blocked))
        after["blocked"] = blocked
        if blocked:
            log.info("portal.requests_blocked", reasons="+".join(blocked))
        if manifest.action_type == "login":
            session.authenticated = binding.authenticated_by(
                url=str(after.get("url", "")), text=str(after.get("text", "")))
        # Die ausgepackte Lesung zuerst, die Aussagen danach. Andersherum
        # ueberschriebe `**after` das gerade bestimmte `authenticated` mit dem
        # Stand von VOR der Anmeldung — die Anmeldung gelaenge, und das Ergebnis
        # behauptete das Gegenteil.
        return {**after, "ok": True, "authenticated": session.authenticated,
                "permit_used": used}

    async def _op_restart(self, _message: dict[str, Any]) -> dict[str, Any]:
        """Raeumt auf und beendet sich. launchd bringt den Dienst wieder hoch.

        Der Weg, auf dem eine neu ausgelieferte Fassung wirksam wird, ohne dass
        jemand ein Administratorpasswort tippt. Sitzungen werden vorher zerstoert
        — ein Neustart darf kein angemeldetes Profil zuruecklassen.
        """
        count = len(self.sessions)
        for session_id in list(self.sessions):
            session = self.sessions.pop(session_id)
            await session.destroy()
        log.info("portal.restart_requested", sessions_destroyed=count,
                 build=self._build())
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
        return {"ok": True, "restarting": True, "sessions_destroyed": count}

    async def _op_close_session(self, message: dict[str, Any]) -> dict[str, Any]:
        session = self._session(message)
        self.sessions.pop(session.session_id)
        await session.destroy()
        return {"ok": True}

    # -- intern --------------------------------------------------------------
    def _session(self, message: dict[str, Any]) -> PortalSession:
        session = self.sessions.get(str(message.get("session_id", "")))
        if session is None:
            raise KeyError("unknown_session")
        if ((session.owner_principal or "owner_principal" in message)
                and (not message.get("owner_principal")
                     or message["owner_principal"] != session.owner_principal)):
            raise KeyError("unknown_session")
        return session

    def _policy(self, url: str) -> bool:
        if self.allow_private_targets:
            return True
        return policy_check(url).allowed

    async def _evaluate(self, session: PortalSession, script: str) -> Any:
        reply = await session.page.cdp.call(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True}, timeout=20)
        if reply.get("exceptionDetails"):
            return None
        return (reply.get("result") or {}).get("value")

    async def _read(self, session: PortalSession) -> dict[str, Any]:  # noqa: D401
        """Seiteninhalt — mit getilgten Geheimnissen und ohne Feldwerte."""
        data = await self._evaluate(session, _VISIBLE_TEXT.replace("%LIMIT%", str(MAX_TEXT)))
        if not isinstance(data, dict):
            return {"url": "", "title": "", "text": "", "truncated": False}
        return {"url": str(data.get("url", "")),
                "title": session.redactor.scrub(str(data.get("title", ""))),
                "text": session.redactor.scrub(str(data.get("text", ""))),
                "truncated": bool(data.get("truncated")),
                "authenticated": session.authenticated}

    async def _reap(self) -> None:
        for session_id, session in list(self.sessions.items()):
            reason = session.expired()
            if reason:
                log.info("portal.session_expired", session_id=session_id, reason=reason)
                self.sessions.pop(session_id, None)
                await session.destroy()

    # -- Betrieb -------------------------------------------------------------
    async def serve(self) -> None:
        """Lauscht am Socket. Redet nur mit dem Core."""
        self._server = P.bind_listener(self.socket_path)
        self._server.setblocking(False)
        loop = asyncio.get_running_loop()
        log.info("portal.worker_listening", socket=self.socket_path,
                 uid=os.getuid(), core_uid=self.core_uid)
        while True:
            connection, _ = await loop.sock_accept(self._server)
            asyncio.create_task(self._converse(connection))

    async def _converse(self, connection: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Zuerst die Kennung, dann erst ein Byte lesen.
            P.authenticate(connection, allowed_uid=self.core_uid)
        except P.ProtocolError as exc:
            log.warning("portal.caller_rejected", detail=str(exc)[:80])
            connection.close()
            return
        connection.setblocking(False)
        try:
            while True:
                message = await loop.run_in_executor(
                    None, P.decode, _Blocking(connection))
                reply = await self.handle(message)
                await loop.sock_sendall(connection, P.encode(reply))
        except (P.ProtocolError, OSError):
            pass
        finally:
            connection.close()

    async def shutdown(self) -> None:
        for session_id in list(self.sessions):
            session = self.sessions.pop(session_id)
            await session.destroy()
        if self._server is not None:
            self._server.close()
            self._server = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


class _Blocking:
    """Ein blockierender Blick auf einen nicht-blockierenden Socket.

    Der Rahmenleser ist synchron und einfach; ihn asynchron nachzubauen waere
    mehr Code fuer dieselbe Aussage. Also laeuft er in einem Thread, und dieser
    Adapter stellt das Blockieren nur fuer ihn wieder her.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def recv(self, count: int) -> bytes:
        self._connection.setblocking(True)
        try:
            return self._connection.recv(count)
        finally:
            self._connection.setblocking(False)


def _manifest_from(data: dict[str, Any]) -> ActionManifest:
    fields = tuple(FieldBinding(name=str(f["name"]), selector=str(f["selector"]),
                                value=str(f.get("value", "")),
                                alias=str(f.get("alias", "")))
                   for f in data.get("fields", ()))
    return ActionManifest(
        portal_id=str(data["portal_id"]), origin=str(data["origin"]),
        page_url=str(data["page_url"]), action_type=str(data["action_type"]),
        target=str(data["target"]), method=str(data["method"]),
        page_signature=str(data["page_signature"]), fields=fields,
        credential_alias=str(data.get("credential_alias", "")),
        version=int(data.get("version", 1)),
        principal=str(data.get("principal", "")))


def main() -> int:  # pragma: no cover - Einstiegspunkt des Dienstes
    import argparse

    from solvio.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="SOLVIO portal worker")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--core-uid", type=int, required=True)
    args = parser.parse_args()
    setup_logging("INFO")
    worker = PortalWorker(socket_path=args.socket, core_uid=args.core_uid)
    try:
        asyncio.run(worker.serve())
    except KeyboardInterrupt:
        asyncio.run(worker.shutdown())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
