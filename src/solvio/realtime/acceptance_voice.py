"""Optional browser/iPhone acceptance bound to the explicitly supplied book.

Normal conversations don't acquire a limit here. Acceptance uses the same
ledger as the prototype, but a thread cuts only this conversation's provider
sockets at the deadline. It must never terminate the productive Core process.
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from urllib.parse import urlsplit

from .acceptance_ledger import BudgetErschoepft, beginnen


def _timer(delay, callback):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class AcceptanceGuard:
    def __init__(self, run, *, end, clock=time.monotonic, timer_factory=_timer,
                 reserve=5.0, tick=0.5):
        self.run = run
        self.clock = clock
        self.started = clock()
        self.allowance = run.ticken()
        self.end = end
        self.reserve = reserve
        self.tick = tick
        self.forced = False
        self.finished = False
        self._lock = threading.Lock()
        self._sockets = []
        self._providers = {}
        self._loop = asyncio.get_running_loop()
        self._deadline = self.started + self.allowance
        # Exactly remaining allowance: cleanup reserve lies INSIDE this timer.
        self._timer = timer_factory(self.remaining, self._hard_stop)
        self._monitor = asyncio.create_task(self._watch())

    @property
    def remaining(self):
        return max(0.0, self._deadline - self.clock())

    def ensure_open(self):
        if self.forced or self.finished or self.remaining <= 0:
            raise BudgetErschoepft("Das Sprach-Abnahmebudget ist abgelaufen.")

    def _remember(self, raw):
        # A duplicate owns this particular socket, not a recycled integer fd.
        duplicate = raw.dup()
        with self._lock:
            if self.forced or self.finished or self.remaining <= 0 or len(self._sockets) >= 32:
                duplicate.close()
                raise BudgetErschoepft("Keine weitere Sprachverbindung im Abnahmerahmen moeglich.")
            self._sockets.append(duplicate)
        return duplicate

    def _forget(self, duplicate):
        with self._lock:
            if duplicate in self._sockets:
                self._sockets.remove(duplicate)
            for key, value in list(self._providers.items()):
                if value is duplicate:
                    del self._providers[key]
        duplicate.close()

    def provider_closed(self, provider):
        """Called after this exact WebSocket's close returned; no retained fd."""
        with self._lock:
            duplicate = self._providers.pop(id(provider), None)
        if duplicate is not None:
            self._forget(duplicate)

    @staticmethod
    def _cut(raw):
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            raw.close()

    def _hard_stop(self):
        # No event loop, ledger lock, network close handshake or provider
        # coroutine is needed for this cut. Book stays unclosed if Core stalls.
        with self._lock:
            if self.finished:
                return
            self.forced = True
            sockets, self._sockets = self._sockets, []
            self._providers.clear()
        for raw in sockets:
            self._cut(raw)
        try:
            self._loop.call_soon_threadsafe(self.end)
        except RuntimeError:  # process loop already gone; physical cut still ran
            pass

    async def _watch(self):
        try:
            while self.remaining > self.reserve:
                self.run.ticken()
                await asyncio.sleep(min(self.tick, self.remaining - self.reserve))
            self.run.ticken()
            self.end()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Lost accounting cannot release an unmetered provider connection.
            self._hard_stop()

    async def connect(self, connector, url, **kwargs):
        """Keep the existing TLS/WebSocket protocol; own TCP before its first await.

        Explicit acceptance currently supports a direct provider route. A
        configured proxy is refused, never silently bypassed or declared bounded.
        """
        self.ensure_open()
        from websockets.proxy import get_proxy
        from websockets.uri import parse_uri
        if get_proxy(parse_uri(url)) is not None:
            raise BudgetErschoepft("Die Sprach-Abnahmegrenze ist fuer diesen Proxyweg nicht eingerichtet.")
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise BudgetErschoepft("Kein gueltiger Sprach-Anbieterweg.")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        loop = asyncio.get_running_loop()
        timeout = min(float(kwargs.pop("open_timeout", 20)), self.remaining)
        async with asyncio.timeout(timeout):
            addresses = await loop.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            self.ensure_open()
            last_error = None
            for family, kind, protocol, _, address in addresses:
                self.ensure_open()
                raw = socket.socket(family, kind, protocol)
                raw.setblocking(False)
                duplicate = None
                try:
                    duplicate = self._remember(raw)
                    await loop.sock_connect(raw, address)
                    self.ensure_open()
                    # The library still supplies TLS verification, Host, auth,
                    # frame protocol, handshake and existing provider behaviour.
                    result = await connector(url, sock=raw, proxy=None,
                                             open_timeout=min(timeout, self.remaining), **kwargs)
                    self.ensure_open()
                    with self._lock:
                        self._providers[id(result)] = duplicate
                    return result
                except OSError as exc:
                    last_error = exc
                    self._cut(raw)
                    if duplicate is not None:
                        self._forget(duplicate)
                except BaseException:
                    self._cut(raw)
                    if duplicate is not None:
                        self._forget(duplicate)
                    raise
            if last_error is not None:
                raise last_error
            raise BudgetErschoepft("Kein erreichbarer Sprach-Anbieterweg.")

    async def finish(self, observation):
        # Called only after actual Session close and worker drain. A forced
        # TCP cut is not upgraded to provider-confirmed closure.
        self._monitor.cancel()
        await asyncio.gather(self._monitor, return_exceptions=True)
        with self._lock:
            confirmed = (observation.get("provider") in {"closed_confirmed", "not_opened"}
                         and observation.get("previous_unconfirmed") == 0 and not self.forced)
            self.finished = True
            sockets, self._sockets = self._sockets, []
            self._providers.clear()
        self._timer.cancel()
        for raw in sockets:
            self._cut(raw)
        if confirmed:
            self.run.beenden("anbieter_abgeschlossen")
        else:
            # A durable unclosed row prevents a restart from reclaiming time.
            self.run.ticken()


def attach(server, *, environment=None):
    env = os.environ if environment is None else environment
    enabled = str(env.get("SOLVIO_ABNAHME", "")).strip().lower() in {"1", "ja", "an", "true", "yes"}
    if not enabled:
        return
    path = str(env.get("SOLVIO_ABNAHME_BUCH", "")).strip()

    async def admit(sess):
        if not path or not os.path.isabs(path):
            raise BudgetErschoepft("Fuer die Sprachabnahme fehlt das ausdruecklich freigegebene Buch.")
        mono, epoch = time.monotonic(), time.time()
        # Monotonic elapsed time, persisted in the inherited wall-time format.
        run = beginnen(path, uhr=lambda: epoch + time.monotonic() - mono)
        sess.acceptance_guard = AcceptanceGuard(run, end=sess.ws.ending.set)
        return True

    async def closed(sess, observation):
        guard = getattr(sess, "acceptance_guard", None)
        if guard is not None:
            await guard.finish(observation)

    server.browser_voice_admission = admit
    server.browser_voice_closed = closed
    # The native endpoint owns the same admission, deadline and close accounting.
    # No hook is installed in ordinary operation and no book is created here.
    server.native_voice_admission = admit
    server.native_voice_closed = closed
