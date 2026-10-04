"""Ephemeral, owner-bound window signaling. No frames, recording or agent work.

Only a sender offer and its recipient's answer are relayed. One video stream,
no audio/data channels/remote input. Browser authorization is re-read from the
existing durable session service; losing signaling also ends the client lease.
"""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field

from aiohttp import web, WSMsgType
from solvio.security.mobile_approval import browser_sessions as B
from .auth import OWNER, owner_browser

CHANNELS = frozenset({'obsidian', 'hermes'})
MAX_MESSAGE = 65536
LEASE_INTERVAL = 1.0
HUB = web.AppKey('solvio_window_share_hub', object)


def _video_direction(sdp):
    """Read one video section's effective direction; never rewrite its SDP.

    RFC 8866 sections 5 and 6.7: a media attribute overrides the session
    default, with at most one direction in either scope. Codec/ICE details
    remain the browsers' concern; ambiguous section boundaries are rejected.
    """
    if any(ord(char)<32 and char not in '\r\n\t' for char in sdp):
        raise ValueError('bad_sdp')
    lines=sdp.replace('\r\n','\n').split('\n')
    if lines[-1]=='':lines.pop()
    if not lines or lines[0]!='v=0':
        raise ValueError('bad_sdp')
    directions={'sendonly','recvonly','sendrecv','inactive'}
    in_media=False
    session_direction=media_direction=None
    for index,line in enumerate(lines):
        if (len(line)<3 or line[1]!='=' or line[0] not in 'vosiuepcbtrzkam'
                or '\r' in line or any(char in line for char in '\x85\u2028\u2029')):
            raise ValueError('bad_sdp')
        field,value=line[0],line[2:]
        if field=='v' and (index!=0 or value!='0'):
            raise ValueError('bad_sdp')
        if field=='m':
            parts=value.split(' ')
            if in_media or parts[0]!='video':
                raise ValueError('video_only')
            if len(parts)<4 or any(not part or any(char.isspace() for char in part) for part in parts):
                raise ValueError('bad_sdp')
            in_media=True
        elif in_media and field not in 'icbka':
            # A session header cannot restart the session inside a media section.
            raise ValueError('bad_sdp')
        if field=='a':
            attribute=value.split(':',1)[0]
            if not attribute or any(char.isspace() for char in attribute):
                raise ValueError('bad_sdp')
            if attribute in directions:
                if value!=attribute:
                    raise ValueError('bad_sdp')
                if in_media:
                    if media_direction is not None:raise ValueError('bad_sdp')
                    media_direction=attribute
                else:
                    if session_direction is not None:raise ValueError('bad_sdp')
                    session_direction=attribute
    if not in_media:raise ValueError('video_only')
    return media_direction or session_direction or 'sendrecv'


@dataclass(eq=False)
class Peer:
    ws: web.WebSocketResponse
    channel: str
    role: str
    actor: B.BrowserActor
    check: object = field(repr=False)
    peer_id: str = field(default_factory=lambda: secrets.token_hex(16))
    offered: set = field(default_factory=set)
    answered: set = field(default_factory=set)


class Hub:
    def __init__(self):
        self.peers = {}
        self.sockets = set()
        self.cleanups = set()
        self.closing = False

    def room(self, peer):
        return [p for p in self.peers.values() if p.channel == peer.channel
                and p.actor.principal == peer.actor.principal and p is not peer]

    async def send(self, peer, data):
        # Bound a slow/disappeared recipient, never retain an SDP queue.
        await peer.check()
        await asyncio.wait_for(peer.ws.send_json(data), 2)

    async def deliver(self, peer, data):
        try:
            await self.send(peer, data)
            return True
        except Exception:
            await peer.ws.close(code=4000, message=b'connection_ended')
            await self.leave(peer)
            return False

    async def join(self, peer):
        if self.closing:
            raise ValueError('core_shutdown')
        room = self.room(peer)
        if (peer.role == 'publisher' and any(p.role == 'publisher' for p in room)
                or peer.role == 'viewer' and sum(p.role == 'viewer' for p in room) >= 2):
            raise ValueError('room_full')
        self.peers[peer.peer_id] = peer  # reserve before first await
        await self.send(peer, {'type':'ready', 'peer':peer.peer_id})
        for other in room:
            publisher, viewer = (peer, other) if peer.role == 'publisher' else (other, peer)
            if publisher.role == 'publisher' and viewer.role == 'viewer':
                await self.deliver(publisher, {'type':'viewer_joined', 'peer':viewer.peer_id})

    async def leave(self, peer):
        if self.peers.pop(peer.peer_id, None) is None:
            return
        for other in self.room(peer):
            other.offered.discard(peer.peer_id)
            other.answered.discard(peer.peer_id)
            try:
                if peer.role == 'publisher':
                    await other.ws.close(code=4000, message=b'sender_ended')
                elif other.role == 'publisher':
                    await self.deliver(other, {'type':'viewer_left', 'peer':peer.peer_id})
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                await other.ws.close(code=4000, message=b'connection_ended')

    async def relay(self, peer, data):
        if self.closing:
            raise ValueError('core_shutdown')
        if not isinstance(data, dict) or set(data) != {'type', 'peer', 'sdp'}:
            raise ValueError('bad_signal')
        kind, target, sdp = data['type'], data['peer'], data['sdp']
        if not isinstance(target, str) or len(target) != 32:
            raise ValueError('bad_recipient')
        recipient = self.peers.get(target)
        if recipient not in self.room(peer) or recipient.role == peer.role:
            raise ValueError('bad_recipient')
        if (not isinstance(sdp, str) or not 1 <= len(sdp) <= 48000
                or '\x00' in sdp):
            raise ValueError('bad_sdp')
        direction = _video_direction(sdp)
        if kind == 'offer' and peer.role == 'publisher':
            if target in peer.offered or direction != 'sendonly':
                raise ValueError('bad_offer')
            peer.offered.add(target)
        elif kind == 'answer' and peer.role == 'viewer':
            if (peer.peer_id not in recipient.offered or target in peer.answered
                    or direction != 'recvonly'):
                raise ValueError('bad_answer')
            peer.answered.add(target)
        else:
            raise ValueError('bad_direction')
        await self.deliver(recipient, {'type':kind, 'peer':peer.peer_id, 'sdp':sdp})


def attach(app):
    hub = Hub()
    app[HUB] = hub

    async def socket(request):
        if not B._origin_allowed(request) or await owner_browser(request) is None:
            raise web.HTTPUnauthorized()
        if hub.closing or len(hub.sockets) >= 8:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE, compress=False)
        hub.sockets.add(ws)
        peer = watch = None
        try:
            await ws.prepare(request)
            first = await asyncio.wait_for(ws.receive(), 5)
            if first.type != WSMsgType.TEXT:
                raise ValueError('hello_required')
            data = json.loads(first.data)
            if (not isinstance(data, dict) or set(data) != {'type', 'role', 'channel', 'csrf'}
                    or data['type'] != 'hello' or data['role'] not in ('publisher', 'viewer')
                    or not isinstance(data['channel'], str) or data['channel'] not in CHANNELS):
                raise ValueError('bad_hello')
            csrf = data['csrf']
            if not isinstance(csrf, str) or len(csrf) != 64:
                raise ValueError('bad_csrf')
            service = app[B._SERVICE]
            token = request.cookies.get(B.COOKIE_NAME, '')
            async def authenticated():
                actor = await service.authenticate(token, csrf_token=csrf)
                if not actor or actor.principal != app[OWNER]:
                    raise ValueError('session_ended')
                return actor
            actor = await authenticated()
            peer = Peer(ws, data['channel'], data['role'], actor, authenticated)

            async def lease():
                try:
                    while not ws.closed:
                        await authenticated()
                        await hub.send(peer, {'type':'lease'})
                        await asyncio.sleep(LEASE_INTERVAL)
                except Exception:
                    await ws.close(code=4401, message=b'session_ended')
            await hub.join(peer)
            watch = asyncio.create_task(lease())
            recent = []
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                await authenticated()
                now = time.monotonic()
                recent = [stamp for stamp in recent if now-stamp < 10]
                if len(recent) >= 8:
                    raise ValueError('too_many_signals')
                recent.append(now)
                await hub.relay(peer, json.loads(message.data))
        except (Exception, asyncio.CancelledError):
            # No payload, cookie, CSRF, SDP, IP address or window title in logs.
            await ws.close(code=4400, message=b'window_connection_ended')
        finally:
            async def finish():
                try:
                    if watch is not None:
                        watch.cancel()
                        await asyncio.gather(watch, return_exceptions=True)
                    if peer is not None:
                        await hub.leave(peer)
                finally:
                    hub.sockets.discard(ws)
            # aiohttp cancels handlers on disconnect, including DURING finally.
            # Keep the entire cleanup alive until every peer was notified/closed.
            cleanup = asyncio.create_task(finish())
            hub.cleanups.add(cleanup)
            cleanup.add_done_callback(hub.cleanups.discard)
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
        return ws

    async def shutdown(_):
        hub.closing = True
        await asyncio.gather(*(ws.close(code=1001, message=b'core_shutdown')
                               for ws in tuple(hub.sockets)), return_exceptions=True)
    app.on_shutdown.append(shutdown)
    async def drain(_):
        while hub.cleanups:
            await asyncio.gather(*tuple(hub.cleanups), return_exceptions=True)
    app.on_cleanup.append(drain)
    app.router.add_get('/v1/dashboard/window', socket)
