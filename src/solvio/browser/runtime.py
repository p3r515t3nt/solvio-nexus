"""Der Browser als Betriebsmittel — von SOLVIO gehalten, nicht vom Modell.

Ein Browser ist teuer zu starten und gefaehrlich, wenn er unbeaufsichtigt lebt.
Beides beantwortet dieses Modul: er startet beim ersten Bedarf, bleibt warm, und
alles, was ihn betrifft — wie viele Tabs es gibt, wann Schluss ist, was nach
einem Absturz passiert — entscheidet SOLVIO.

Zur Identitaet: eine Seite heisst hier `pg-…`, und diesen Namen vergibt SOLVIO.
Chromes eigene Target-Kennung steht daneben. Dieselbe Trennung wie beim tiefen
Executor, aus demselben Grund: wer die Kennung vergibt, besitzt die Sache.

Zum Absturz: der Browser ist ein eigener Prozess. Stirbt er, stirbt eine
Faehigkeit — nicht der Core. Beim naechsten Aufruf wird ein neuer gestartet, und
die alten Seiten sind ehrlich verloren statt scheinbar noch da.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any

from solvio.browser.cdp import BrowserProcess, BrowserUnavailable
from solvio.browser.page import BrowserPage
from solvio.logging_setup import get_logger

log = get_logger("browser")

#: Wie viele Seiten gleichzeitig offen sein duerfen. Eine Seite, die Popups
#: erzeugt, soll den Rechner nicht fuellen.
MAX_PAGES = 4

#: Wie lange ein unbenutzter Browser wartet, bevor er beendet wird.
IDLE_SECONDS = 300.0

#: Lebenszyklus-Namen (§16). Stabil, damit ein HUD sie spaeter lesen kann.
STARTING = "browser_starting"
NAVIGATING = "navigating"
PAGE_LOADED = "page_loaded"
EXTRACTING = "extracting"
INTERACTING = "interacting"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass(frozen=True)
class BrowserEvent:
    """Ein Schritt im Leben eines Aufrufs. Identitaet ist die Sequenz, nie die Uhr."""

    sequence: int
    call_id: str
    page_id: str
    phase: str
    detail: str = ""


def new_page_id() -> str:
    """Die SOLVIO-Kennung einer Seite. Entsteht hier, nicht im Browser."""
    return "pg-" + secrets.token_hex(6)


class BrowserRuntime:
    """Haelt den Browserprozess und die offenen Seiten."""

    def __init__(self, *, recorder=None, max_pages: int = MAX_PAGES) -> None:
        self.process: BrowserProcess | None = None
        self.pages: dict[str, BrowserPage] = {}
        self.max_pages = max_pages
        self._recorder = recorder
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._cancelled: set[str] = set()

    # -- Beobachtung ---------------------------------------------------------
    def emit(self, call_id: str, page_id: str, phase: str, detail: str = "") -> BrowserEvent:
        self._sequence += 1
        event = BrowserEvent(self._sequence, call_id, page_id, phase, detail)
        if self._recorder is not None:
            try:
                self._recorder(event)
            except Exception as exc:  # noqa: BLE001 - Beobachtung stoert nie
                log.error("browser.recorder_failed", kind=type(exc).__name__)
        else:
            log.info("browser." + phase, call_id=call_id, page_id=page_id,
                     sequence=event.sequence, detail=detail)
        return event

    # -- Prozess -------------------------------------------------------------
    async def ensure(self) -> BrowserProcess:
        """Startet den Browser, wenn noetig. Nach einem Absturz eben neu."""
        async with self._lock:
            if self.process is not None and self.process.alive():
                return self.process
            if self.process is not None:
                # Er ist gestorben. Die alten Seiten sind fort — das ehrlich zu
                # sagen ist besser, als Kennungen zu behalten, hinter denen
                # nichts mehr steht.
                log.warning("browser.process_died", pages=len(self.pages))
                self.pages.clear()
                await self.process.stop()
            process = BrowserProcess()
            await process.start()
            self.process = process
            return process

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.alive()

    async def health(self) -> dict[str, Any]:
        return {"running": self.running,
                "version": self.process.version if self.process else "",
                "pages": len(self.pages), "max_pages": self.max_pages}

    # -- Seiten --------------------------------------------------------------
    async def open_page(self) -> tuple[str, BrowserPage]:
        process = await self.ensure()
        if len(self.pages) >= self.max_pages:
            # Aelteste zuerst. Eine Seite, die Tabs erzeugt, verdraengt damit
            # ihre eigenen — statt die Grenze zu sprengen.
            oldest = next(iter(self.pages))
            await self.close_page(oldest)
        cdp = await process.new_page_socket()
        page = BrowserPage(cdp)
        await page.prepare()
        # Der Zustand beginnt bei dem, was wirklich offen ist (`about:blank`) —
        # nicht bei einer leeren Zeichenkette. Sonst sieht ein Klick, der nichts
        # bewegt, wie eine Navigation aus, und eine Pruefung feuert ohne Anlass.
        page.state.url = await page.current_url()
        page_id = new_page_id()
        self.pages[page_id] = page
        return page_id, page

    def page(self, page_id: str) -> BrowserPage | None:
        return self.pages.get(page_id)

    async def close_page(self, page_id: str) -> bool:
        page = self.pages.pop(page_id, None)
        if page is None:
            return False
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
        return True

    # -- Abbruch -------------------------------------------------------------
    def cancel(self, page_id: str) -> bool:
        """Sofort verbindlich. Was danach zurueckkommt, aendert nichts mehr.

        Dieselbe Reihenfolge wie beim tiefen Executor: erst gilt es hier, dann
        wird es dem Browser mitgeteilt. Eine spaet eintreffende Seite kann einen
        abgebrochenen Aufruf nicht wiederbeleben, weil der Aufruf seine Antwort
        nicht mehr aus ihr bezieht.
        """
        self._cancelled.add(page_id)
        page = self.pages.get(page_id)
        if page is not None:
            page.cancel()
            return True
        return False

    def is_cancelled(self, page_id: str) -> bool:
        return page_id in self._cancelled

    async def stop(self) -> None:
        for page_id in list(self.pages):
            await self.close_page(page_id)
        if self.process is not None:
            await self.process.stop()
            self.process = None


def available() -> bool:
    """Gibt es hier ueberhaupt einen Browser?"""
    from solvio.browser.cdp import chrome_binary
    try:
        chrome_binary()
    except BrowserUnavailable:
        return False
    return True
