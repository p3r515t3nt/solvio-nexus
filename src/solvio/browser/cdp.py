"""Ein eigener, sehr kleiner Draht zum Browser.

Playwright liegt hier nicht, `browser-use` ebenfalls nicht — und beides wurde
geprueft, bevor es hier selbst gebaut wurde. `browser-use` traegt fuenf
LLM-SDKs, PostHog-Telemetrie, einen Cloud-Client und, entscheidend fuer ein
aiohttp-Projekt, ein exaktes `aiohttp==3.14.3` in seinen Pflichtabhaengigkeiten.
Fuer eine DOM-Schicht ist das der falsche Preis. Was SOLVIO wirklich braucht,
sind neun CDP-Kommandos ueber einen Websocket — und `websockets` und `aiohttp`
liegen ohnehin im Baum.

Das Gegenteil von „nicht erfunden hier": die teuren Lehren aus `browser-use`
werden uebernommen, nur eben als Wissen und nicht als Abhaengigkeit. Zwei davon
stehen weiter unten im Code, an der Stelle, an der sie wirken.

Der Prozess ist ein eigener Prozess. Ein abstuerzender Browser darf den Core
nicht mitnehmen, und ein Browser, der haengt, muss beendbar sein, ohne dass
jemand den Core anfasst.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
from typing import Any

import aiohttp

from solvio.logging_setup import get_logger

log = get_logger("browser")

#: Der einzige Chromium auf dieser Maschine. Bewusst ein fester Pfad statt einer
#: Suche im `PATH`: welcher Browser gestartet wird, ist eine
#: Sicherheitsentscheidung und kein Zufall der Umgebung.
CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "/Applications/Chromium.app/Contents/MacOS/Chromium",
          "/usr/bin/chromium", "/usr/bin/google-chrome")

#: Startschalter. Jeder einzelne hat einen Grund; keiner schaltet eine
#: Sicherheitsfunktion des Browsers ab — insbesondere bleibt Chromes eigene
#: Sandbox an (kein `--no-sandbox`).
FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--no-service-autorun",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    # Kein Zugriff auf den echten Schluesselbund des Nutzers. Ohne das fragt
    # Chrome unter Umstaenden nach Anmeldedaten, die es hier nie geben soll.
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
    "--window-size=1280,900",
    "--mute-audio",
)

#: Das echte Profil des Nutzers. Steht hier ausschliesslich, damit es abgelehnt
#: werden kann: es enthaelt Sitzungen, Passwoerter und Verlauf, und nichts davon
#: hat in einer Automatisierung etwas zu suchen.
FORBIDDEN_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome")

START_TIMEOUT = 20.0


class BrowserUnavailable(RuntimeError):
    """Kein Browser — dann eben keine Browser-Faehigkeit."""


def chrome_binary() -> str:
    for path in CHROME:
        if os.path.exists(path):
            return path
    raise BrowserUnavailable("no chromium-family browser found")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CdpSocket:
    """Ein Websocket zum Browser, mit Antwortzuordnung und Ereignissen."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._socket: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list] = {}
        self._reader: asyncio.Task | None = None
        self.closed = False

    async def open(self) -> None:
        from websockets.asyncio.client import connect
        # Kein Grössenlimit: eine seriöse Nachrichtengrenze steht in `page.py`
        # als Zeichenzahl, nicht hier als Byte-Abbruch mitten im JSON.
        self._socket = await connect(self.url, max_size=None, open_timeout=10)
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    future = self._pending.pop(int(message["id"]), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                method = str(message.get("method", ""))
                for handler in list(self._handlers.get(method, ())):
                    try:
                        handler(message.get("params") or {})
                    except Exception as exc:  # noqa: BLE001 - ein Zuhoerer stoert nie
                        log.error("browser.handler_failed", kind=type(exc).__name__)
        except Exception:  # noqa: BLE001 - Verbindungsende ist kein Fehler
            pass
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserUnavailable("connection closed"))
            self._pending.clear()

    def on(self, method: str, handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    async def call(self, method: str, params: dict[str, Any] | None = None, *,
                   timeout: float = 20.0) -> dict[str, Any]:
        if self.closed or self._socket is None:
            raise BrowserUnavailable("connection closed")
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._socket.send(json.dumps(
            {"id": message_id, "method": method, "params": params or {}}))
        try:
            reply = await asyncio.wait_for(future, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            self._pending.pop(message_id, None)
            raise
        if "error" in reply:
            raise CdpError(str(reply["error"].get("message", "cdp error"))[:160])
        return reply.get("result") or {}

    async def close(self) -> None:
        self.closed = True
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:  # noqa: BLE001
                pass
            self._socket = None


class CdpError(RuntimeError):
    """Der Browser hat ein Kommando abgelehnt. Nie an das Modell durchgereicht."""


class BrowserProcess:
    """Ein eigener Chrome auf einem Wegwerfprofil."""

    def __init__(self, *, binary: str = "", profile_dir: str = "") -> None:
        self.binary = binary or chrome_binary()
        self.profile_dir = profile_dir or tempfile.mkdtemp(prefix="solvio-browser-")
        if os.path.realpath(self.profile_dir).startswith(os.path.realpath(FORBIDDEN_PROFILE)):
            # Kein `assert`: unter `python -O` waere die Pruefung weg — und die
            # Sitzungen des Nutzers genau dort, wo sie am meisten schaden.
            raise BrowserUnavailable("refusing to drive the user's own Chrome profile")
        self.port = 0
        self.process: Any = None
        self.version = ""

    async def start(self) -> None:
        self.port = free_port()
        argv = [self.binary, *FLAGS,
                f"--user-data-dir={self.profile_dir}",
                # Ausdruecklich an die Loopback-Adresse. Der Debug-Port kennt
                # keine Anmeldung: wer ihn erreicht, steuert den Browser.
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self.port}",
                "about:blank"]
        self.process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT
        while loop.time() < deadline:
            if self.process.returncode is not None:
                raise BrowserUnavailable("browser exited during startup")
            version = await self._version()
            if version:
                self.version = version
                log.info("browser.started", pid=self.process.pid, port=self.port,
                         version=version, profile="disposable")
                return
            await asyncio.sleep(0.25)
        await self.stop()
        raise BrowserUnavailable("browser did not become reachable")

    async def _version(self) -> str:
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://127.0.0.1:{self.port}/json/version") as reply:
                    if reply.status != 200:
                        return ""
                    body = await reply.json()
        except Exception:  # noqa: BLE001 - noch nicht oben
            return ""
        return str(body.get("Browser", ""))

    async def new_page_socket(self) -> CdpSocket:
        """Oeffnet einen neuen Tab und liefert den Draht dorthin."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(
                    f"http://127.0.0.1:{self.port}/json/new?about:blank") as reply:
                if reply.status >= 400:
                    raise BrowserUnavailable(f"cannot open tab: HTTP {reply.status}")
                target = await reply.json()
        url = str(target.get("webSocketDebuggerUrl", ""))
        if not url:
            raise BrowserUnavailable("tab without a debugger url")
        cdp = CdpSocket(url)
        await cdp.open()
        return cdp

    async def targets(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://127.0.0.1:{self.port}/json/list") as reply:
                    entries = await reply.json()
        except Exception:  # noqa: BLE001
            return []
        return [e for e in entries if e.get("type") == "page"]

    async def close_target(self, target_id: str) -> None:
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.get(f"http://127.0.0.1:{self.port}/json/close/{target_id}")
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def stop(self) -> None:
        """Erst hoeflich, dann bestimmt — und das Wegwerfprofil verschwindet."""
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                self.process.kill()
                await self.process.wait()
        self.process = None
        # Chrome's helper processes can still write into the profile for a moment after
        # the main process exits; a single pass then left the folder — cookies included —
        # on disk (gemessen 26.09.2026: 1 von 3 Laeufen). Retry briefly until it is gone.
        for _ in range(30):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            if not os.path.exists(self.profile_dir):
                break
            await asyncio.sleep(0.1)
