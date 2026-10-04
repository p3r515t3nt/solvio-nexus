"""Der Sprachweg des iPhones — auf dem Weg, der schon vertraut ist.

Es entsteht hier **kein neuer Vertrauensanker und kein neuer Port**. Dieselbe
TLS-Verbindung mit gepinntem Zertifikat, dieselbe Geraeteregistrierung, dieselbe
Transportkennung wie beim Freigabeweg und beim Kontrollzentrum. Angehaengt wird
an die bereits gebaute Anwendung, statt den Freigabe-Gateway zu veraendern — das
ist genau das Vorgehen, das das Kontrollzentrum freigegeben hat
(`src/solvio/control_center/routes.py`), und aus demselben Grund: der Gateway
ist heikel genug, er soll so bleiben, wie er freigegeben wurde.

**Warum nicht der Satellitenport 8766?** Der ist Klartext — `satellite_auth.py`
sagt das selbst: „Not TLS, not a PKI, not device attestation." Ein Telefon
traegt sein Mikrofon durch die Wohnung; rohes Sprachaudio unverschluesselt zu
senden waere ein Rueckschritt hinter das, was der Freigabeweg laengst kann. Dazu
kaeme ein zweites Geheimnis, das irgendwie auf das Telefon muesste — die
Kopplungsnutzlast ist eingefroren, und den Nutzer einen Schluessel abtippen zu
lassen ist ausgeschlossen. Und die Satellitenkennung kennt keine Sperre: ein
verlorenes Telefon waere nur durch Editieren einer Datei und einen Neustart zu
entziehen. `verify_transport_cred` prueft die Sperrliste bei jedem Aufruf.

**Was die Transportkennung beweist und was nicht.** Sie beweist, dass die
Verbindung vom registrierten, attestierten, nicht gesperrten Geraet des
Besitzers kommt. Sie beweist **keine Freigabe**. Wer spricht, hat damit nichts
genehmigt: eine folgenreiche Handlung geht weiterhin als Freigabe auf das iPhone
und braucht Face ID und eine frische App-Attest-Aussage. Dieses Modul kennt den
Freigabepfad gar nicht und bietet keinen Weg dorthin an.

Dass eine Transportkennung damit auch ein *Gespraech* eroeffnen darf — und ein
Gespraech harmlose Faehigkeiten ausloesen kann — ist eine Ausweitung dessen, was
sie bisher durfte. Sie ist aufgeschrieben, nicht nebenbei passiert:
`docs/decisions/ADR-0018-iphone-ist-sprachendpunkt-nicht-autoritaet.md`.

**Was hier NICHT noch einmal gebaut wird.** Die Sitzung ist die freigegebene
`Session`, die Nachrichtenschleife ist `pump_endpoint` — beides aus
`realtime/core_server.py`, beides dasselbe, was der Satellit benutzt.
Gespraechsbesitz, Barge-in, Vorlauf, Werkzeugschleife, Wiederaufbau und Fristen
bleiben unveraendert. Was ein zweiter Endpunkt brauchte, war erstaunlich wenig,
weil `Session` von ihrem Transport nur eine einzige Methode benutzt (`ws.send`).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any

from aiohttp import WSMsgType, web

from solvio import voice_session_proof as VSP
from solvio.logging_setup import get_logger

log = get_logger("voice_endpoint")

PATH = "/v1/voice"

#: Groesse eines einzelnen Rahmens. 20 ms PCM16 bei 16 kHz sind 640 Byte; das
#: hier ist grosszuegig fuer eine Sammelsendung und trotzdem weit von allem
#: entfernt, was nach Missbrauch aussieht.
#:
#: Die Grenze MUSS hier stehen: `client_max_size` der Anwendung begrenzt
#: HTTP-Koerper, nicht WebSocket-Rahmen, und aiohttp erlaubt sonst 4 MiB. Ein
#: 4-MiB-Rahmen waere ein synchroner Resample-Lauf ueber zwei Millionen Samples
#: auf demselben Eventloop, der die Freigaben bedient.
MAX_FRAME = 64 * 1024

#: Wie oft die Berechtigung waehrend eines laufenden Gespraechs neu geprueft
#: wird.
#:
#: `verify_transport_cred` laeuft sonst genau einmal, beim Verbindungsaufbau —
#: ein gesperrtes Geraet behielte seine offene Sitzung, und eine Sperre ist
#: ausdruecklich als endgueltig gemeint. Die Pruefung ist ein Datenbankblick,
#: kein Netzverkehr; alle 20 Sekunden kostet sie nichts und schliesst genau
#: diese Luecke.
RECHECK_SECONDS = 20.0

#: Rueckfall, wenn der Server die Einstellung nicht kennt. Der Satellit
#: behaelt in jedem Fall seinen eigenen Wert.
#:
#: Der Weg dahin ging ueber `high` (staendig unterbrochen, sobald ein
#: Fernseher lief), `low` (von 19 Anlaeufen kamen 5 durch) und `medium`.
#: Zurueck bei `high`, und das ist kein Kreis: die Ruecksicht war noetig,
#: solange das Telefon ALLES weiterschickte, was sein Mikrofon hoerte.
#: Inzwischen haelt es Raumgeraeusch selbst zurueck und unterbricht selbst,
#: sobald wirklich jemand redet. Was `eagerness` jetzt noch bestimmt, ist vor
#: allem, wie lange SOLVIO wartet, bevor er glaubt, dass der Mensch fertig
#: ist — und das Warten war das, was sich langsam anfuehlte.
PHONE_EAGERNESS = "high"


#: Der Schliesscode, wenn die App einen Chat verlangt hat, der sich nicht binden
#: laesst — kein degradierter Betrieb, keine Linger-Auswahl als Ersatz (§7 C3).
BINDING_REFUSED_CODE = 4401
BINDING_REFUSED_REASON = b"conversation_not_bindable"


class _EndpointSocket:
    """Die eine Methode, die `Session` von einem Transport braucht.

    `Session` ruft ausschliesslich `await self.ws.send(...)` auf — nachgezaehlt
    sieben Stellen, alle `send`, einmal mit JSON als Zeichenkette und einmal mit
    PCM16 als Bytes. Genau das bildet dieser Adapter ab. Eine Fassade mit mehr
    Methoden waere eine Einladung, spaeter mehr zu benutzen.

    Ein Sendeversuch auf eine schon geschlossene Verbindung ist hier kein
    Fehler: die Sitzung raeumt beim Schliessen noch auf, und ein `session_end`
    ins Leere darf den Abbau nicht kippen.
    """

    __slots__ = ("_ws", "_closed")

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, payload: Any) -> None:
        if self._closed or self._ws.closed:
            return
        try:
            if isinstance(payload, (bytes, bytearray, memoryview)):
                await self._ws.send_bytes(bytes(payload))
            else:
                await self._ws.send_str(str(payload))
        except (ConnectionResetError, RuntimeError) as exc:
            self._closed = True
            log.info("voice_endpoint.send_after_close", kind=type(exc).__name__)


class _BoundSocket(_EndpointSocket):
    """Derselbe Adapter — und ein Schliesscode, den nur ein verweigerter Chat setzt.

    Wurde ein Chat verlangt und scheitert seine Bindung beim Oeffnen der Sitzung
    (`ConversationBindingError`), endet die Sitzung mit `session_end` und
    `conversation_mode == "refused"`. Dieser Adapter schliesst den Socket dann
    mit 4401 statt ihn offen zu lassen: die App darf keine ungebundene Sitzung
    als Ersatz bekommen.
    """

    __slots__ = ("session", "ending_task", "ending")

    def __init__(self, ws: web.WebSocketResponse) -> None:
        super().__init__(ws)
        self.session: Any = None
        self.ending_task: asyncio.Task | None = None
        self.ending = asyncio.Event()

    async def send(self, payload: Any) -> None:
        if not isinstance(payload, str) or self.session is None:
            await super().send(payload)
            return
        try:
            value = json.loads(payload)
        except ValueError:
            await super().send(payload)
            return
        if isinstance(value, dict) and value.get("type") == "session_end":
            # This may be the timer/reader, rather than the endpoint pump.
            self.ending_task = asyncio.current_task()
            self.ending.set()
        await super().send(payload)
        if (isinstance(value, dict) and value.get("type") == "session_end"
                and getattr(self.session, "conversation_mode", "") == "refused"
                and not self._ws.closed):
            log.warning("voice_endpoint.conversation_binding_refused",
                        session_id=getattr(self.session, "session_id", ""))
            self._closed = True
            await self._ws.close(code=BINDING_REFUSED_CODE, message=BINDING_REFUSED_REASON)


async def _drain_cleanup(coroutine):
    """Keep the existing Session's cleanup owned across handler cancellation."""
    owned = asyncio.create_task(coroutine)
    cancelled = False
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError:
            cancelled = True
    result = owned.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _owner_device(request: web.Request) -> str | None:
    """Dieselbe Pruefung wie beim Freigabeweg — nicht eine eigene.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht. Sie prueft Geraetestatus,
    Attestierung, Sperre, Umgebung und die Kennung in konstanter Zeit.
    """
    from solvio.security.mobile_approval.gateway import _authed_device
    return await _authed_device(request)


def principal_for(device_id: str) -> str:
    """Wer spricht — abgeleitet aus der bewiesenen Geraetekennung.

    Das ist **nicht** der Principal, unter dem eine Freigabe abgelegt wird: den
    setzt `approval_gateway` unveraendert auf den Besitzer, unabhaengig davon,
    wer gefragt hat. Dieser Name landet in `voice_trust` (bewiesener Anrufer
    ja/nein), im Protokoll und in der duennen Zusammenfassung einer Anfrage.

    Das Praefix ist Absicht: eine Telefonsitzung soll im Journal nie mit einer
    Satellitensitzung zu verwechseln sein.
    """
    short = "".join(ch for ch in (device_id or "") if ch.isalnum() or ch in "._-")[:12]
    return f"iphone-{short}" if short else "iphone"


def attach(app: web.Application, server: Any) -> web.Application:
    """Haengt den Sprachweg an die bestehende Anwendung.

    `server` ist der laufende `CoreServer`. Er wird nicht veraendert; gelesen
    werden nur sein Sitzungsriegel und die Sitzungsfabrik.
    """
    app["voice_core_server"] = server
    connections: set[web.WebSocketResponse] = set()
    app["voice_websockets"] = connections

    async def shutdown_voice(app):
        # Closing the transport lets the existing endpoint finally drain its
        # Session (provider, tools and persisted turns), before memory closes.
        await asyncio.gather(*(ws.close(code=1001, message=b"core_stopping")
                               for ws in tuple(connections)), return_exceptions=True)

    app.on_shutdown.append(shutdown_voice)
    app.router.add_get(PATH, _handle)
    log.info("voice_endpoint.attached", path=PATH)
    return app


def _long_capability_notice(sess: Any, server: Any, ws: web.WebSocketResponse):
    """Sagt dem Endpunkt Bescheid, wenn eine LANGE Faehigkeit begonnen hat.

    „Lang" ist keine Schaetzung und kein Zeitgeber: es ist die
    Ausfuehrungsklasse `DEEP` aus dem Faehigkeitsvertrag — dieselbe Angabe, die
    auch bedeutet, dass so eine Aufgabe nicht im Sprach-Turn zu Ende laeuft.
    Genau einmal je Sitzung; eine zweite Recherche im selben Gespraech ist keine
    neue Nachricht wert.

    Die Nachricht ist AUSKUNFT. Sie traegt keinen Zustand des Cores, keine
    Kennung und keinen Fachbegriff — die App macht daraus einen Satz in
    Produktsprache.
    """
    sent = False

    async def notice(capability: str) -> None:
        nonlocal sent
        if sent:
            return
        dispatcher = getattr(server, "dispatcher", None)
        router = getattr(dispatcher, "capabilities", None)
        spec = router.spec(capability) if router is not None else None
        if spec is None or getattr(spec.execution_class, "value", "") != "deep":
            return
        sent = True
        try:
            await ws.send_str(json.dumps({"type": "notice", "kind": "deep_work"}))
        except Exception as exc:  # noqa: BLE001
            log.info("voice_endpoint.notice_failed", kind=type(exc).__name__)
        log.info("voice_endpoint.deep_work", session_id=sess.session_id,
                 capability=capability)

    return notice


async def _frames(ws: web.WebSocketResponse):
    """Die aiohttp-Nachrichten als das, was `pump_endpoint` erwartet.

    Genau zwei Sorten kommen durch: `bytes` fuer Audio und `str` fuer JSON.
    Alles andere — Schliessen, Fehler, Ping — beendet die Schleife, statt
    stillschweigend etwas anderes zu bedeuten.
    """
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            yield msg.data
        elif msg.type == WSMsgType.TEXT:
            yield msg.data
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING,
                          WSMsgType.CLOSED):
            return


async def _acceptance_frames(ws, transport, sess, admission):
    """One explicit acceptance start; internal recovery retains that admission.

    Ordinary native sockets keep their existing multi-session protocol. Only
    acceptance sockets end after this session, so a second start cannot replace
    the original guard or reclaim its deadline.
    """
    started = False
    async for frame in _frames(ws):
        if transport.ending.is_set():
            return
        if isinstance(frame, bytes):
            if started:
                yield frame
            continue
        body = json.loads(frame)
        kind = body.get("type") if isinstance(body, dict) else None
        if kind == "session_start":
            if started or set(body) != {"type"} or await admission(sess) is not True:
                return
            if transport.ending.is_set():
                return
            started = True
        elif kind == "session_end":
            if started:
                yield frame
            return
        elif not started:
            continue
        yield frame


async def _revocation_watch(request: web.Request, device_id: str,
                            ws: web.WebSocketResponse, session_id: str) -> None:
    """Bleibt dieses Geraet berechtigt? Solange das Gespraech laeuft, immer wieder.

    Eine Sperre soll sofort wirken und nicht erst, wenn der Mensch von selbst
    auflegt. Faellt die Pruefung, wird die Verbindung geschlossen — die Sitzung
    raeumt daraufhin im `finally` des Handlers ab.
    """
    try:
        while not ws.closed:
            await asyncio.sleep(RECHECK_SECONDS)
            if ws.closed:
                return
            if await _owner_device(request) is None:
                log.warning("voice_endpoint.revoked_mid_session",
                            device=device_id[:12], session_id=session_id)
                await ws.close(code=4401, message=b"unauthorized")
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keine Berechtigung aus einem Prueffehler ableiten
        log.info("voice_endpoint.recheck_failed", kind=type(exc).__name__)
        await ws.close(code=4401, message=b"authorization_unavailable")



async def _session_proof(request: web.Request, ws: web.WebSocketResponse,
                         device_id: str, *, session=None) -> bool:
    """Fordert den App-Attest-Sitzungsbeweis an und prueft ihn.

    Kein neuer Endpunkt, kein zweites Geheimnis: die Nonce geht ueber die
    bereits offene, TLS-gepinnte Verbindung, und geprueft wird mit dem
    eingefrorenen Verifizierer gegen den eingeschriebenen Schluessel.

    Ein Telefon, das den Rahmen nicht kennt, antwortet nicht — nach drei
    Sekunden laeuft das Gespraech ohne den Beweis weiter. Deshalb wartet der
    Core hier, bevor irgendetwas anderes passiert: eine spaeter eintreffende
    Antwort duerfte die Herkunft eines schon begonnenen Turns nicht mehr
    aendern.
    """
    control_plane = request.app.get("control_plane")
    if control_plane is None:
        return False
    core_id = str(getattr(control_plane, "core_instance_id", "") or "")
    if not core_id:
        return False
    nonces: VSP.SessionNonces = request.app.setdefault(
        "voice_session_nonces", VSP.SessionNonces())
    nonce = nonces.issue(device_id)
    try:
        await ws.send_str(json.dumps({"type": "session_challenge",
                                      "protocol_version": VSP.BINDING_PROTOCOL_VERSION,
                                      "core_instance_id": core_id,
                                      "session_nonce": nonce}))
        message = await asyncio.wait_for(ws.receive(), timeout=VSP.PROOF_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionResetError, RuntimeError):
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="no_answer")
        return False
    if message.type is not web.WSMsgType.TEXT:
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="wrong_frame")
        return False
    try:
        payload = json.loads(message.data)
    except Exception:  # noqa: BLE001 - eine unlesbare Antwort ist kein Beweis
        payload = None
    if type(payload) is dict and session is not None:
        # Der Chatwunsch wird VOR jeder weiteren Pruefung vermerkt: wer einen Chat
        # verlangt, laeuft nie ungebunden weiter — auch nicht, wenn Assertion oder
        # Typ des Rahmens unbrauchbar sind (Review Runde 6, B6-H-1; Runde 4, B-H3).
        wanted = str(payload.get("conversation_id", "") or "")
        if wanted:
            session.requested_conversation_id = wanted[:64]
    try:
        assertion = base64.b64decode(str(payload.get("assertion", "")), validate=True)
    except Exception:  # noqa: BLE001 - eine unlesbare Antwort ist kein Beweis
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="unreadable")
        return False
    if str(payload.get("type", "")) != "session_assertion":
        return False
    # N8/C3: der Chat, den die App fuer diese Sitzung verlangt — Teil der
    # signierten Bindung, nie eine nachtraegliche Behauptung. Er wird an der
    # Sitzung vermerkt, damit der Endpunkt VOR der Nachrichtenschleife das
    # Eigentum prueft (mit dem Geraete-Principal aus dem Beweis, nie mit
    # `satellite_id`). Nur eine Kennung in der vom Core gepraegten Form zaehlt.
    requested = str(payload.get("conversation_id", "") or "")
    if requested and not re.fullmatch(r"c-[0-9a-f]{16}", requested):
        log.info("voice_endpoint.proof_rejected", device=device_id[:12],
                 kind="conversation_id")
        # Die App hat einen Chat VERLANGT — auch mit unbrauchbarer Kennung endet die
        # Sitzung dann mit 4401 statt ungebunden weiterzulaufen (Review Runde 4, B-H3;
        # vermerkt oben, die unbrauchbare Kennung selbst — kein Sentinel).
        return False
    if session is not None:
        session.requested_conversation_id = requested
    # Die Nonce zaehlt genau einmal, und nur fuer das Geraet, das sie bekam.
    if not nonces.consume(str(payload.get("session_nonce", "")), device_id):
        log.info("voice_endpoint.proof_rejected", device=device_id[:12],
                 kind="nonce")
        return False
    from solvio.voice_task_session import VerifiedAppTaskSession, device_generation
    before = await device_generation(control_plane, device_id) if session is not None else None
    ok = await VSP.verify_session_proof(
        control_plane, device_id=device_id, core_instance_id=core_id,
        session_nonce=nonce, assertion=assertion, conversation_id=requested)
    if ok and session is not None:
        after = await device_generation(control_plane, device_id)
        if before is not None and before == after and control_plane.core_instance_id == core_id:
            proof = VerifiedAppTaskSession(before[0], device_id, core_id, session.session_id,
                nonce, before, control_plane,
                lambda: not ws.closed and not getattr(session, "_closing", False)
                and not getattr(session, "_stopping", False)
                and getattr(session, "app_task_session", None) is proof)
            session.app_task_session = proof
    log.info("voice_endpoint.session_proof", device=device_id[:12], proven=ok)
    return ok


async def _attach_requested_conversation(sess: Any, server: Any, ws: web.WebSocketResponse) -> bool:
    """Den von der App verlangten Chat an die Sitzung binden — oder 4401.

    Bedingungen (§7 C3): ein bestandener Sitzungsbeweis (nur dann gibt es einen
    Principal) UND der Chat gehoert diesem Principal (aktiv, explizit). Sonst
    schliesst der Socket mit `conversation_not_bindable`; es gibt keine
    Linger-Auswahl als Ersatz und keine Provider-Sitzung.
    """
    requested = str(getattr(sess, "requested_conversation_id", "") or "")
    if not requested:
        return True
    proof = getattr(sess, "app_task_session", None)
    principal = str(getattr(proof, "principal", "") or "") if proof is not None else ""
    store = getattr(server, "conversations", None)
    owned = False
    if principal and store is not None:
        try:
            owned = bool(await asyncio.to_thread(store.conversation_owned, requested, principal, for_write=True))
        except Exception as exc:  # noqa: BLE001 - ein unlesbarer Speicher bindet nichts
            log.info("voice_endpoint.conversation_check_failed", kind=type(exc).__name__)
            owned = False
    if not owned:
        log.warning("voice_endpoint.conversation_not_bindable", session_id=sess.session_id,
                    proven=bool(principal))
        sess.conversation_mode = "refused"
        await ws.close(code=BINDING_REFUSED_CODE, message=BINDING_REFUSED_REASON)
        return False
    sess.bound_conversation_id = requested
    log.info("voice_endpoint.conversation_bound", session_id=sess.session_id,
             conversation_id=requested)
    return True


async def _handle(request: web.Request) -> web.WebSocketResponse:
    """Eine Sprachverbindung vom registrierten iPhone.

    Reihenfolge mit Absicht: **erst** die Geraetepruefung, dann der
    Protokollwechsel. Ein nicht registriertes Geraet bekommt eine gewoehnliche
    HTTP-Absage und nie einen offenen Socket — dieselbe Haltung wie beim
    Satelliten, wo vor der Authentifizierung nichts Teures passiert.
    """
    peer = request.remote or "?"
    device_id = await _owner_device(request)
    if device_id is None:
        log.warning("voice_endpoint.rejected", peer=peer, reason="unauthorized")
        return web.json_response({"error": "unauthorized"}, status=401)

    server = request.app.get("voice_core_server")
    if server is None:
        log.error("voice_endpoint.no_server", peer=peer)
        return web.json_response({"error": "voice_unavailable"}, status=503)

    # Ein Gespraech zur Zeit — derselbe prozessweite Riegel wie fuer den
    # Satelliten, und aus demselben Grund: es gibt EINE Anbietersitzung und EIN
    # Gespraech. Wer zu spaet kommt, bekommt eine Auskunft und keinen rohen
    # Abbruch; die App sagt es dem Menschen so, wie es ist.
    if getattr(server, "_closing", False):
        return web.json_response({"error": "core_stopping"}, status=503)
    if getattr(server, "_busy", False):
        log.info("voice_endpoint.busy", peer=peer, device=device_id[:12])
        return web.json_response({"error": "voice_busy"}, status=409)

    ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20.0)
    await ws.prepare(request)
    # AppRunner shutdown may have passed its connection snapshot while the
    # handshake yielded. Such a connection never acquires a Session afterward.
    if getattr(server, "_closing", False):
        await ws.close(code=1001, message=b"core_stopping")
        return ws
    if getattr(server, "_busy", False):
        await ws.close(code=4409, message=b"voice_busy")
        return ws
    request.app["voice_websockets"].add(ws)

    t_connected = time.monotonic()
    server._busy = True
    from solvio.realtime.core_server import Session, describe_exception, pump_endpoint

    from solvio.realtime.core_server import create_session
    transport = _BoundSocket(ws)
    sess = create_session(server, transport)
    transport.session = sess
    sess.t_connected = t_connected
    sess.t_auth = time.monotonic()
    # Die Geraetepruefung hat diese Kennung bewiesen. Ab hier traegt die
    # Sitzung sie, damit eine Faehigkeit weiss, WER fragt — genauso wie beim
    # Satelliten, nur mit einem Namen, den man davon unterscheiden kann.
    sess.satellite_id = principal_for(device_id)
    # Ein Telefon hoert seinen Raum mit, ein Satellit steht darin. `high` laesst
    # SOLVIO auf dem Telefon bei jedem Geraeusch anhalten — gemessen mit einem
    # leise laufenden Fernseher daneben. `low` heisst nicht taub: die
    # Erkennung bleibt semantisch, sie ist nur zurueckhaltender damit, ein
    # Geraeusch fuer eine Ansprache zu halten.
    sess.eagerness = getattr(server, "phone_eagerness", PHONE_EAGERNESS)
    # Das Telefon hat einen Bildschirm. Was im Hintergrund entstanden ist,
    # steht dort im Tab „Hinweise" mit Zaehler — es muss nicht zu Beginn eines
    # Gespraechs vorgelesen werden, das der Mensch gerade mit einer Frage
    # eroeffnet hat.
    sess.mention_proactive = False
    # DIE HERKUNFTSKLASSE. Sie steht hier und nur hier, nach der bewiesenen
    # Geraetepruefung — sie ist Transportwahrheit, nie eine Angabe des Modells.
    #
    # Und sie ist nicht mehr nur fuers Protokoll: Adaptive Memory lernt
    # ausschliesslich aus dieser Klasse. Ein Telefon ist entsperrt, registriert,
    # attestiert und jemand haelt es in der Hand — ein Raummikrofon hoert
    # jeden, der zufaellig im Zimmer redet, den Fernseher eingeschlossen.
    # Belegt ist auch hier das GERAET und nicht die STIMME; der Unterschied ist
    # die Absicht, mit der eine Sitzung geoeffnet wird.
    sess.channel = "voice_iphone"
    sess.observe_authenticated_device(device_id)
    # DER SITZUNGSBEWEIS. Er entscheidet, ob diese Sitzung die reduzierte
    # iPhone-Zeile der Freigabematrix traegt oder die vorsichtige Raum-Zeile.
    #
    # Fail-DOWN, nicht fail-closed: eine App, die nicht antwortet, bekommt ein
    # ganz gewoehnliches Gespraech — nur eben mit dem Face-ID-Verhalten, das sie
    # heute schon hat. Niemand verliert etwas, das er hatte.
    sess.interactive_proof = await _session_proof(request, ws, device_id, session=sess)
    # Lange Faehigkeiten sichtbar machen — als AUSKUNFT, nicht als Zustand des
    # Cores. Kein Zeitgeber, keine Schaetzung.
    sess.on_capability_started = _long_capability_notice(sess, server, ws)
    log.info("voice_endpoint.connected", peer=peer, device=device_id[:12],
             principal=sess.satellite_id, session_id=sess.session_id)

    watch = asyncio.create_task(_revocation_watch(request, device_id, ws,
                                                  sess.session_id))
    acceptance_workers = []
    try:
        # N8/C3: ein verlangter Chat wird VOR der Nachrichtenschleife gebunden —
        # oder die Verbindung endet hier. Fail-closed NUR fuer die Bindung: ohne
        # Chatwunsch bleibt das Fail-down von oben. Der Principal ist der des
        # Sitzungsbeweises; ohne Beweis gibt es keinen, und `iphone-…` ist keiner.
        if not await _attach_requested_conversation(sess, server, ws):
            return ws
        admission = getattr(server, "native_voice_admission", None)
        if admission is None:
            await pump_endpoint(sess, _frames(ws))
        else:
            pump = asyncio.create_task(pump_endpoint(sess,
                _acceptance_frames(ws, transport, sess, admission)))
            ending = asyncio.create_task(transport.ending.wait())
            acceptance_workers = [pump, ending]
            done, _ = await asyncio.wait(acceptance_workers,
                                        return_when=asyncio.FIRST_COMPLETED)
            if pump in done:
                await pump
    except Exception as exc:  # noqa: BLE001 - eine Sprachverbindung darf den Core nie kippen
        log.error("voice_endpoint.error", session_id=sess.session_id,
                  stage=sess.open_stage, **describe_exception(exc))
    finally:
        handler = asyncio.current_task()
        async def cleanup():
            try:
                watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)
                ending = (getattr(sess, "_closing_task", None)
                          if acceptance_workers else None) or transport.ending_task
                # A pump currently inside close owns the provider-final/drain
                # sequence. Never cancel that owner merely because its early
                # session_end woke the acceptance deadline/endpoint waiter.
                others = [t for t in acceptance_workers if t is not ending]
                for worker in others:
                    worker.cancel()
                await asyncio.gather(*others, return_exceptions=True)
                if acceptance_workers:
                    # A timer/reader can enter close while cancellation yields.
                    ending = getattr(sess, "_closing_task", None) or ending
                if ending is not None and ending is not handler and not ending.done():
                    await asyncio.gather(ending, return_exceptions=True)
                await sess.close(reason="disconnect")
                await sess._drain()
                sess.audio_disconnected()
                closed_hook = getattr(server, "native_voice_closed", None)
                if closed_hook is not None:
                    # Reuse the existing generation-bound audio observation;
                    # a socket close alone is not a confirmed provider ending.
                    from solvio.browser_voice_endpoint import _closed_observation
                    await closed_hook(sess, _closed_observation(sess, device_id))
            finally:
                sess.app_task_session = None
                server._busy = False
                request.app["voice_websockets"].discard(ws)
                if not ws.closed:
                    await ws.close()
                log.info("voice_endpoint.disconnected", session_id=sess.session_id,
                         seconds=int(time.monotonic() - t_connected))
        await _drain_cleanup(cleanup())
    return ws
