"""Eine Seite, die SOLVIO gehoert.

Der Unterschied zu „ein Browser, den das Modell bedient" steckt in drei
Entscheidungen, die alle hier getroffen werden:

**Jede Anfrage wird angesehen.** Nicht nur die URL, die jemand genannt hat. Eine
Weiterleitung kennt vorher niemand, und sie ist der bequemste Weg, einen Browser
ins lokale Netz zu schicken — deshalb haengt die Pruefung am Netzverkehr und
nicht an der Eingabe.

**Nur GET.** Das ist die strukturelle Antwort auf „was, wenn ein Klick etwas
ausloest". Auf die Beschriftung eines Knopfes ist kein Verlass — „Weiter" kann
ein Verweis sein und „Mehr erfahren" ein Absendeknopf. Die DOM-Semantik wird
vorher gefragt, damit die Ablehnung ehrlich benannt werden kann; die
Netzwerkregel ist der Boden darunter, der auch dann traegt, wenn die Semantik
etwas uebersieht.

**Ziele haben Namen, keine Pfade.** Das Modell benennt Rolle und zugaenglichen
Namen — „Knopf Preise" —, nicht `div > ul:nth-child(3) > a`. Ein erzeugter Pfad
ist beim naechsten Seiten-Deploy falsch, und er verleitet zum Raten. Passen
mehrere Elemente, ist das eine Mehrdeutigkeit und keine Auswahlaufgabe.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any

from solvio.browser import js
from solvio.browser.cdp import BrowserUnavailable, CdpError, CdpSocket
from solvio.browser.policy import UrlVerdict, check
from solvio.logging_setup import get_logger

log = get_logger("browser")

#: Alles, was eine Seite an Ressourcen des Core verbrauchen darf. Eine feindliche
#: Seite ist kein Sonderfall, sondern der Normalfall, auf den man auslegt.
NAV_TIMEOUT = 25.0
LOAD_SETTLE = 1.2
MAX_TEXT_CHARS = 12000
MAX_LINKS = 120
MAX_TARGETS = 150
MAX_REDIRECTS = 10
MAX_NODES = 60000

#: Nur lesende Anfragen. Ein Formular abzuschicken, etwas hochzuladen oder eine
#: Buchung auszuloesen geht ueber POST/PUT/PATCH/DELETE — und nichts davon
#: verlaesst diesen Browser.
SAFE_METHODS = frozenset({"GET", "HEAD"})


@dataclass
class PageState:
    """Was zuletzt gelesen wurde. Kein Zustand des Modells, Zustand von SOLVIO."""

    url: str = ""
    title: str = ""
    targets: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    download_attempted: bool = False
    redirects: int = 0


class NavigationBlocked(RuntimeError):
    """Die Policy hat das Ziel abgelehnt. `reason` ist der stabile Grund."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail[:160]


class BrowserPage:
    """Ein Tab unter SOLVIOs Aufsicht."""

    def __init__(self, cdp: CdpSocket, *, target_id: str = "",
                 resolver=None, write_gate=None, origin_gate=None) -> None:
        self.cdp = cdp
        self.target_id = target_id
        # Ohne Torwaechter bleibt es bei „nur Lesen" — genau wie in Browser V1.
        # Der angemeldete Portalweg reicht hier sein Erlaubnisbuch herein; damit
        # ist die Voreinstellung weiterhin Verweigern, und eine Ausnahme entsteht
        # nur aus einer Freigabe. Kein Schalter macht POST global erlaubt.
        self._write_gate = write_gate
        # Ohne Torwaechter gilt nur die Netz-Policy: oeffentlich ja, privat nein.
        # Das ist fuer oeffentliches Surfen richtig. Ein angemeldeter Vorgang
        # braucht enger: dort reicht die BINDUNG eine Erlaubnisliste herein, und
        # alles ausserhalb — Fehlermelder, Zaehldienste, fremde Einbettungen —
        # geht nicht hinaus.
        self._origin_gate = origin_gate
        self.state = PageState()
        self._verdicts: dict[str, UrlVerdict] = {}
        self._resolver = resolver
        self._cancelled = False
        self._prepared = False

    # -- Aufbau --------------------------------------------------------------
    async def prepare(self) -> None:
        """Schaltet die Aufsicht ein, bevor die erste Seite geladen wird."""
        if self._prepared:
            return
        await self.cdp.call("Page.enable")
        await self.cdp.call("Runtime.enable")
        # Herunterladen gibt es nicht. Weder in ein Verzeichnis noch „nur kurz":
        # eine Datei, die eine fremde Seite auf die Platte legt, ist ein anderes
        # Produkt als eine Webseite lesen.
        try:
            await self.cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except CdpError:
            await self.cdp.call("Page.setDownloadBehavior", {"behavior": "deny"})
        self.cdp.on("Page.downloadWillBegin", self._on_download)
        self.cdp.on("Fetch.requestPaused", self._on_request)
        # Ohne Muster heisst: jede Anfrage. Teurer als eine Auswahl, aber eine
        # Auswahl waere eine Liste dessen, woran man gerade gedacht hat.
        await self.cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        self._prepared = True

    def cancel(self) -> None:
        """Ab hier passiert nichts mehr. Lokal verbindlich, sofort."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    # -- Aufsicht ueber den Netzverkehr --------------------------------------
    def _on_download(self, params: dict[str, Any]) -> None:
        self.state.download_attempted = True
        log.info("browser.download_denied")

    def _on_request(self, params: dict[str, Any]) -> None:
        asyncio.create_task(self._decide(params))

    async def _decide(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId", ""))
        request = params.get("request") or {}
        url = str(request.get("url", ""))
        method = str(request.get("method", "GET")).upper()
        try:
            if self._cancelled:
                await self._fail(request_id, "Aborted")
                return
            if method not in SAFE_METHODS:
                # Der Boden unter der DOM-Pruefung. Was hier ankommt, hat die
                # semantische Pruefung ueberstanden und ist trotzdem ein Schreiben.
                allowed, why = (self._write_gate(url=url, method=method)
                                if self._write_gate is not None else (False, "read_only"))
                if not allowed:
                    self.state.blocked.append("side_effect_blocked")
                    log.info("browser.write_blocked", method=method, reason=why)
                    await self._fail(request_id, "BlockedByClient")
                    return
            verdict = await self._verdict(url)
            if not verdict.allowed:
                self.state.blocked.append(verdict.reason)
                if verdict.reason == "private_network_blocked":
                    log.warning("browser.private_target_blocked", host=verdict.host)
                await self._fail(request_id, "BlockedByClient")
                return
            if self._origin_gate is not None and not self._origin_gate(url):
                self.state.blocked.append("origin_not_allowed")
                log.info("browser.foreign_origin_blocked", host=verdict.host)
                await self._fail(request_id, "BlockedByClient")
                return
            await self.cdp.call("Fetch.continueRequest", {"requestId": request_id},
                                timeout=10)
        except (CdpError, BrowserUnavailable):
            pass                      # Anfrage ist fort; nichts mehr zu entscheiden
        except Exception as exc:      # noqa: BLE001 - die Aufsicht crasht nie den Core
            log.error("browser.interception_failed", kind=type(exc).__name__)

    async def _fail(self, request_id: str, reason: str) -> None:
        try:
            await self.cdp.call("Fetch.failRequest",
                                {"requestId": request_id, "errorReason": reason},
                                timeout=10)
        except (CdpError, BrowserUnavailable):
            pass

    async def _verdict(self, url: str) -> UrlVerdict:
        """Die Policy-Antwort fuer diese URL, gemerkt je Herkunft.

        Gemerkt wird bewusst pro Schema+Host und nicht pro vollstaendiger URL:
        eine Seite mit hundert Bildern derselben Herkunft soll nicht hundert
        Namensaufloesungen ausloesen.
        """
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(url)
            key = f"{parts.scheme}://{parts.hostname}:{parts.port or ''}"
        except ValueError:
            key = url[:120]
        cached = self._verdicts.get(key)
        if cached is not None:
            return cached
        if self._resolver is not None:
            verdict = check(url, resolver=self._resolver)
        else:
            verdict = await asyncio.to_thread(check, url)
        self._verdicts[key] = verdict
        return verdict

    # -- Navigation ----------------------------------------------------------
    async def navigate(self, url: str) -> str:
        """Faehrt eine oeffentliche Adresse an. Wirft, wenn die Policy Nein sagt."""
        verdict = await self._verdict(url)
        if not verdict.allowed:
            raise NavigationBlocked(verdict.reason, verdict.host)

        loaded = asyncio.Event()
        self.cdp.on("Page.loadEventFired", lambda _p: loaded.set())
        try:
            reply = await self.cdp.call("Page.navigate", {"url": url},
                                        timeout=NAV_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise NavigationBlocked("timeout") from exc
        error = str(reply.get("errorText", ""))
        if error:
            # `net::ERR_BLOCKED_BY_CLIENT` ist unsere eigene Absage — die soll
            # nicht als Netzproblem erscheinen.
            if "BLOCKED_BY_CLIENT" in error or "ABORTED" in error:
                raise NavigationBlocked(
                    self.state.blocked[-1] if self.state.blocked
                    else "private_network_blocked")
            raise NavigationBlocked("navigation_failed", error)

        try:
            await asyncio.wait_for(loaded.wait(), timeout=NAV_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            pass                       # Teilweise geladen ist besser als gar nichts
        await asyncio.sleep(LOAD_SETTLE)   # JS-nachgeladener Inhalt

        if self.state.download_attempted:
            raise NavigationBlocked("download_not_supported")

        final = await self.current_url()
        # Dritte Schicht: was tatsaechlich erreicht wurde. Sollte die
        # Abfangschicht je eine Weiterleitung verpassen, endet es hier.
        final_verdict = await self._verdict(final)
        if not final_verdict.allowed:
            raise NavigationBlocked(final_verdict.reason, final_verdict.host)
        self.state.url = final
        return final

    async def current_url(self) -> str:
        try:
            reply = await self.cdp.call(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True}, timeout=10)
        except (CdpError, TimeoutError, asyncio.TimeoutError):
            return self.state.url
        return str((reply.get("result") or {}).get("value") or self.state.url)

    async def back(self) -> str:
        history = await self.cdp.call("Page.getNavigationHistory", timeout=10)
        index = int(history.get("currentIndex", 0))
        entries = history.get("entries") or []
        if index <= 0:
            raise NavigationBlocked("navigation_failed", "no history")
        entry = entries[index - 1]
        verdict = await self._verdict(str(entry.get("url", "")))
        if not verdict.allowed:
            raise NavigationBlocked(verdict.reason, verdict.host)
        await self.cdp.call("Page.navigateToHistoryEntry",
                            {"entryId": entry.get("id")}, timeout=NAV_TIMEOUT)
        await asyncio.sleep(LOAD_SETTLE)
        self.state.url = await self.current_url()
        return self.state.url

    # -- Lesen ---------------------------------------------------------------
    async def _evaluate(self, expression: str, *, timeout: float = 20.0) -> Any:
        reply = await self.cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            timeout=timeout)
        if reply.get("exceptionDetails"):
            raise CdpError("page script failed")
        return (reply.get("result") or {}).get("value")

    async def read(self) -> dict[str, Any]:
        """Sichtbarer Text der Seite, so wie ein Mensch ihn saehe."""
        data = await self._evaluate(js.with_limit(js.EXTRACT_TEXT, MAX_TEXT_CHARS))
        if not isinstance(data, dict):
            raise CdpError("extraction returned no object")
        if int(data.get("node_count", 0)) > MAX_NODES:
            data["oversized"] = True
        self.state.title = str(data.get("title", ""))
        self.state.url = str(data.get("url", self.state.url))
        return data

    async def links(self) -> list[dict[str, Any]]:
        data = await self._evaluate(js.with_limit(js.EXTRACT_LINKS, MAX_LINKS))
        return data if isinstance(data, list) else []

    async def targets(self) -> list[dict[str, Any]]:
        """Bedienbare Elemente mit Rolle und Namen — und nur die sichtbaren."""
        data = await self._evaluate(js.with_limit(js.EXTRACT_TARGETS, MAX_TARGETS))
        self.state.targets = data if isinstance(data, list) else []
        return self.state.targets

    # -- Klicken -------------------------------------------------------------
    async def inspect(self, ref: int) -> dict[str, Any]:
        data = await self._evaluate(js.with_ref(js.INSPECT_TARGET, ref))
        return data if isinstance(data, dict) else {"found": False}

    async def click(self, ref: int) -> dict[str, Any]:
        """Klickt genau ein Element — nachdem gefragt wurde, was das bewirkt."""
        if self._cancelled:
            raise NavigationBlocked("cancelled")
        info = await self.inspect(ref)
        if not info.get("found"):
            raise NavigationBlocked("element_not_found")
        if info.get("uploads"):
            raise NavigationBlocked("side_effect_blocked", "file upload")
        if info.get("downloads"):
            raise NavigationBlocked("download_not_supported", "download link")
        if info.get("submits"):
            raise NavigationBlocked("side_effect_blocked", "form submit")
        if info.get("in_form") and str(info.get("form_method", "")).lower() != "get":
            raise NavigationBlocked("side_effect_blocked", "non-get form")
        if info.get("href"):
            verdict = await self._verdict(str(info["href"]))
            if not verdict.allowed:
                raise NavigationBlocked(verdict.reason, verdict.host)

        await self._evaluate(js.with_ref(js.SCROLL_TO, ref))
        info = await self.inspect(ref)
        x, y = float(info.get("x", 0)), float(info.get("y", 0))
        for kind in ("mousePressed", "mouseReleased"):
            await self.cdp.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "clickCount": 1}, timeout=10)
        await asyncio.sleep(LOAD_SETTLE)
        if self.state.download_attempted:
            raise NavigationBlocked("download_not_supported")
        final = await self.current_url()
        # Nur pruefen, wenn sich wirklich etwas bewegt hat. Ein Klick, der die
        # Seite nur aufklappt, hat kein neues Ziel — und die alte Adresse noch
        # einmal zu bewerten heisst, eine bereits getroffene Entscheidung ohne
        # neuen Anlass zu wiederholen.
        if final != self.state.url:
            final_verdict = await self._verdict(final)
            if not final_verdict.allowed:
                raise NavigationBlocked(final_verdict.reason, final_verdict.host)
            self.state.url = final
        return {"url": self.state.url}

    async def wait_for(self, seconds: float) -> None:
        """Begrenztes Warten. Eine Seite bestimmt nicht, wie lange SOLVIO wartet."""
        await asyncio.sleep(max(0.0, min(float(seconds), 10.0)))

    async def screenshot(self) -> bytes:
        reply = await self.cdp.call("Page.captureScreenshot", {"format": "png"},
                                    timeout=20)
        return base64.b64decode(str(reply.get("data", "")) or "")

    async def close(self) -> None:
        await self.cdp.close()
