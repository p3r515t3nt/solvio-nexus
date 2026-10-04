"""Owner browser transport for the existing Core Session, not a second voice loop.

The one-time socket nonce is transient; the existing approval database owns
browser identity/revocation. No device credential or Face-ID assertion is minted.
Only explicit session_start reaches the provider. Browser session_end is final.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import time

from aiohttp import WSMsgType, web

from solvio.dashboard.auth import owner_browser
from solvio.security.mobile_approval import browser_sessions as B
from solvio.voice_endpoint import _EndpointSocket, _long_capability_notice, MAX_FRAME, PHONE_EAGERNESS
from solvio.logging_setup import get_logger

log = get_logger("browser_voice_endpoint")
PATH = "/v1/browser/voice"
SESSION_PATH = PATH + "/session"
PROTOCOL_VERSION = 1
NONCE_SECONDS = 30.0
AUTH_SECONDS = 3.0
START_SECONDS = 10.0
RECHECK_SECONDS = 1.0
MAX_NONCES = 128
STATE = web.AppKey("solvio_browser_voice_endpoint", object)


@dataclass(frozen=True)
class _Binding:
    principal: str
    browser_session_id: str
    core_instance_id: str
    expires_at: float
    #: N8/C3: der Chat, den das Ticket bindet — leer heisst Linger wie bisher.
    conversation_id: str = ""


_CONVERSATION = re.compile(r"c-[0-9a-f]{16}\Z")


class _Nonces:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.pending = {}

    def issue(self, actor, core_id, *, conversation_id=""):
        now = self.clock()
        self.pending = {k:v for k,v in self.pending.items() if v.expires_at > now}
        if len(self.pending) >= MAX_NONCES:
            return None
        nonce = secrets.token_urlsafe(32)
        binding = _Binding(actor.principal, actor.session_id, core_id, now + NONCE_SECONDS,
                           str(conversation_id or ""))
        self.pending[hashlib.sha256(nonce.encode()).digest()] = binding
        return nonce, binding.expires_at

    def consume(self, nonce, actor, core_id):
        """Die verbrauchte Bindung — oder None. Ein Ticket gilt genau einmal."""
        if not B._valid_secret(nonce):
            return None
        binding = self.pending.pop(hashlib.sha256(nonce.encode()).digest(), None)
        if binding is None or binding.expires_at <= self.clock() or (
                binding.principal, binding.browser_session_id, binding.core_instance_id) != (
                actor.principal, actor.session_id, core_id):
            return None
        return binding


class _BrowserSocket(_EndpointSocket):
    def __init__(self, ws, connection_id):
        super().__init__(ws)
        self.connection_id = connection_id
        self.session = None
        self.ending = asyncio.Event()
        self.ending_task = None

    async def send(self, payload):
        if isinstance(payload, str):
            try:
                value = json.loads(payload)
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("type") == "session_end":
                self.ending_task = asyncio.current_task()
                self.ending.set()
            elif isinstance(value, dict) and value.get("type") == "session_ready" and self.session is not None:
                row = _audio_row(self.session, self.connection_id)
                value.update(session_id=self.session.session_id, connection_id=self.connection_id,
                             generation=row.get("generation"),
                             # Selected by Core, never by the browser or a
                             # provider-controlled ready payload.
                             voice_mode="full_duplex" if self.session.server.model == "gpt-live-1"
                             else "turn_based")
                payload = json.dumps(value)
        await super().send(payload)


class _Endpoint:
    def __init__(self, server):
        self.server = server
        self.nonces = _Nonces()
        self.closing = False
        self.connections = set()
        self.handlers = set()

    def stopping(self):
        return self.closing or getattr(self.server, "_closing", False)


def _reply(body, *, status=200):
    return B._response(body, status=status)


async def _owner(request):
    # Unlike ordinary read-only HTTP, a WS must have exactly one trusted Origin.
    if not B._origin_allowed(request):
        return None
    return await owner_browser(request)


async def _same_owner(request, actor):
    current = await _owner(request)
    return current is not None and (current.session_id, current.principal) == (
        actor.session_id, actor.principal)


async def _issue(request):
    actor = await owner_browser(request, mutating=True)
    if actor is None:
        return _reply({"error":"unauthorized"}, status=401)
    endpoint = request.app[STATE]
    if endpoint.stopping():
        return _reply({"error":"core_stopping"}, status=503)
    if getattr(endpoint.server, "_busy", False):
        return _reply({"error":"voice_busy"}, status=409)
    try:
        raw = await request.read()
        body = json.loads(raw)
    except (ValueError, web.HTTPException):
        return _reply({"error":"invalid_request"}, status=400)
    # `{}` wie bisher — oder `{"conversation_id": "c-…"}` fuer einen expliziten Chat (§7 C3).
    if len(raw) > 256 or type(body) is not dict or set(body) not in (set(), {"conversation_id"}):
        return _reply({"error":"invalid_request"}, status=400)
    conversation_id = str(body.get("conversation_id", "") or "")
    if body and (type(body["conversation_id"]) is not str or not _CONVERSATION.fullmatch(conversation_id)):
        return _reply({"error":"invalid_request"}, status=400)
    if conversation_id:
        store = getattr(endpoint.server, "conversations", None)
        try:
            owned = store is not None and bool(await asyncio.to_thread(
                store.conversation_owned, conversation_id, actor.principal, for_write=True))
        except Exception:  # noqa: BLE001 - ein unlesbarer Speicher bindet nichts
            owned = False
        if not owned:
            return _reply({"error":"unknown_conversation"}, status=404)
    # The body read yielded. Recheck admission before issuing the bound nonce.
    if endpoint.stopping() or not await _same_owner(request, actor):
        return _reply({"error":"authorization_unavailable"}, status=401)
    service = request.app[B._SERVICE]
    issued = endpoint.nonces.issue(actor, service.core_instance_id, conversation_id=conversation_id)
    if issued is None:
        return _reply({"error":"voice_pending_limit"}, status=429)
    nonce, expires_at = issued
    return _reply({"nonce":nonce, "expires_at":expires_at, "websocket_path":PATH,
        "protocol_version":PROTOCOL_VERSION,
        "audio":{"encoding":"pcm_s16le", "sample_rate":16000, "channels":1}}, status=201)


async def _frames(request, endpoint, actor, ws, transport, sess):
    """Validate browser lifecycle; all actual audio/control handling stays in pump_endpoint."""
    started = False
    deadline = time.monotonic() + START_SECONDS
    while not ws.closed and not transport.ending.is_set() and not endpoint.stopping():
        try:
            message = await asyncio.wait_for(ws.receive(), max(0, deadline - time.monotonic())) \
                if not started else await ws.receive()
        except asyncio.TimeoutError:
            return
        if message.type == WSMsgType.BINARY:
            if not started or len(message.data) % 2:
                return
            yield message.data
            continue
        if message.type != WSMsgType.TEXT:
            return
        try:
            body = json.loads(message.data)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        kind = body.get("type")
        if kind == "session_end":
            return
        if kind == "session_start":
            if started or set(body) != {"type"} or not await _same_owner(request, actor):
                return
            admission = getattr(endpoint.server, "browser_voice_admission", None)
            if admission is not None and await admission(sess) is not True:
                return
            # Admission may have booked a bounded start while yielding. Its
            # final accounting still runs if identity expired in that window.
            if not await _same_owner(request, actor):
                return
            if endpoint.stopping() or transport.ending.is_set():
                return
            started = True
        elif not started or kind not in {"barge_in", "heard", "underrun", "pong"}:
            return
        yield message.data


async def _watch(request, actor, ws):
    while not ws.closed:
        await asyncio.sleep(RECHECK_SECONDS)
        if not await _same_owner(request, actor):
            return


def _audio_row(sess, connection_id):
    observer = getattr(sess.server, "audio_observations", None)
    snapshot = observer.snapshot() if observer is not None else {}
    return next((r for r in snapshot.get("devices", [])
        if r.get("device_id") == connection_id and r.get("session_id") == sess.session_id
        and r.get("generation") == sess.open_attempts), {})


def _closed_observation(sess, connection_id):
    row = _audio_row(sess, connection_id)
    provider = row.get("provider") if row.get("conversation") == "ended" else "unknown"
    if provider not in {"closed_confirmed", "not_opened"}:
        provider = "unknown"
    return {"type":"session_closed", "session_id":sess.session_id,
        "connection_id":connection_id, "generation":row.get("generation"),
        "provider":provider, "previous_unconfirmed":row.get("previous_unconfirmed")}


async def _drain(coroutine):
    """Repeated handler cancellation cannot abandon its Session/worker cleanup."""
    from solvio.voice_endpoint import _drain_cleanup
    return await _drain_cleanup(coroutine)


async def _handle(request):
    actor = await _owner(request)
    if actor is None:
        return _reply({"error":"unauthorized"}, status=401)
    endpoint = request.app[STATE]
    if endpoint.stopping():
        return _reply({"error":"core_stopping"}, status=503)
    if getattr(endpoint.server, "_busy", False):
        return _reply({"error":"voice_busy"}, status=409)
    ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20.0)
    await ws.prepare(request)
    # Shutdown can have passed its connection snapshot during prepare().
    if endpoint.stopping():
        await ws.close(code=1001, message=b"core_stopping")
        return ws
    task = asyncio.current_task()
    endpoint.handlers.add(task)
    endpoint.connections.add(ws)
    sess = None
    owned = False
    workers = []
    connection_id = "browser:" + secrets.token_hex(16)
    transport = _BrowserSocket(ws, connection_id)
    try:
        try:
            frame = await asyncio.wait_for(ws.receive(), AUTH_SECONDS)
            body = json.loads(frame.data) if frame.type == WSMsgType.TEXT and len(frame.data) <= 256 else None
        except (ValueError, asyncio.TimeoutError):
            body = None
        service = request.app[B._SERVICE]
        binding = None
        if (not isinstance(body, dict) or set(body) != {"type", "nonce"}
                or body["type"] != "session_auth" or not await _same_owner(request, actor)
                or (binding := endpoint.nonces.consume(body["nonce"], actor, service.core_instance_id)) is None):
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        if endpoint.stopping() or getattr(endpoint.server, "_busy", False):
            await ws.close(code=4409, message=b"voice_unavailable")
            return ws
        # No await between the last shared check and acquiring the shared owner.
        endpoint.server._busy = True
        owned = True
        from solvio.realtime.core_server import Session, pump_endpoint
        from solvio.browser_voice_session import verified_browser_task_session
        from solvio.realtime.core_server import create_session
        sess = create_session(endpoint.server, transport)
        transport.session = sess
        sess.channel = "voice_browser"
        sess.satellite_id = actor.principal
        sess.eagerness = getattr(endpoint.server, "phone_eagerness", PHONE_EAGERNESS)
        sess.mention_proactive = False
        sess.t_auth = time.monotonic()
        sess.observe_authenticated_device(connection_id)
        sess.browser_task_session = await verified_browser_task_session(actor=actor,
            service=service, session_id=sess.session_id, session_nonce=body["nonce"],
            alive=lambda: not ws.closed and not endpoint.stopping() and not transport.ending.is_set()
                and not sess._closing and not sess._stopping)
        if (sess.browser_task_session is None or endpoint.stopping()
                or not await _same_owner(request, actor)):
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        # N8/C3: der im Ticket gebundene Chat wird VOR der Nachrichtenschleife an die
        # Sitzung gebunden — mit dem Principal des Sitzungsbeweises. Ist er inzwischen
        # weg oder fremd, endet die Verbindung hier: keine Linger-Auswahl als Ersatz.
        if binding.conversation_id:
            store = getattr(endpoint.server, "conversations", None)
            try:
                bindable = store is not None and bool(await asyncio.to_thread(
                    store.conversation_owned, binding.conversation_id, sess.browser_task_session.principal, for_write=True))
            except Exception:  # noqa: BLE001
                bindable = False
            if not bindable:
                log.warning("browser_voice.conversation_not_bindable", session_id=sess.session_id)
                sess.conversation_mode = "refused"
                await ws.close(code=4401, message=b"conversation_not_bindable")
                return ws
            sess.bound_conversation_id = binding.conversation_id
        sess.on_capability_started = _long_capability_notice(sess, endpoint.server, ws)
        await ws.send_json({"type":"session_authenticated", "session_id":sess.session_id,
            "connection_id":connection_id, "protocol_version":PROTOCOL_VERSION})
        workers = [asyncio.create_task(pump_endpoint(sess, _frames(request, endpoint, actor, ws, transport, sess))),
                   asyncio.create_task(_watch(request, actor, ws)),
                   asyncio.create_task(transport.ending.wait())]
        await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
    except Exception as exc:
        log.info("browser_voice.failed", kind=type(exc).__name__)
    finally:
        async def cleanup():
            try:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if sess is not None:
                    # A provider timer/reader may already be closing. That
                    # precise owner must finish; calling close concurrently
                    # would hit Session's early _closing return.
                    ending = transport.ending_task
                    if ending is not None and not ending.done():
                        await asyncio.gather(ending, return_exceptions=True)
                    await sess.close(reason="endpoint")
                    await sess._drain()
                    sess.audio_disconnected()
                    observation = _closed_observation(sess, connection_id)
                    closed_hook = getattr(endpoint.server, "browser_voice_closed", None)
                    if closed_hook is not None:
                        await closed_hook(sess, dict(observation))
                    await transport.send(json.dumps(observation))
            finally:
                if sess is not None:
                    sess.browser_task_session = None
                if owned:
                    endpoint.server._busy = False
                endpoint.connections.discard(ws)
                if sess is not None and sess.conversation_mode == "refused" and not ws.closed:
                    # Ein verlangter Chat liess sich beim Oeffnen nicht binden (§7 C3):
                    # 4401 statt 1000, damit die App keinen Ersatz oeffnet.
                    await ws.close(code=4401, message=b"conversation_not_bindable")
                await ws.close()
        try:
            await _drain(cleanup())
        finally:
            endpoint.handlers.discard(task)
    return ws


def attach(app, server):
    endpoint = _Endpoint(server)
    app[STATE] = endpoint
    async def shutdown(_app):
        endpoint.closing = True
        endpoint.nonces.pending.clear()
        await asyncio.gather(*(ws.close(code=1001, message=b"core_stopping")
                               for ws in tuple(endpoint.connections)), return_exceptions=True)
        await asyncio.gather(*tuple(endpoint.handlers), return_exceptions=True)
    app.on_shutdown.append(shutdown)
    app.router.add_post(SESSION_PATH, _issue)
    app.router.add_get(PATH, _handle)
    return app
