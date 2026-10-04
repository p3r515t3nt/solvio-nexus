"""Die Aussenflaeche von Hermes — und nur die.

Bewusst HTTP und bewusst kein `import hermes`. Wer die inneren Klassen eines
fremden Projekts importiert, hat es geheiratet: jede Umbenennung dort ist dann
ein Bruch hier, und ein Austausch des Executors kostet eine Umschreibung statt
einer Konfigurationszeile. Was dieses Modul benutzt, sind sechs Aufrufe, die
Hermes selbst als stabile Schnittstelle fuehrt — dieselben, die ein fremdes UI
benutzen wuerde.

Zwei Eigenarten der Ereignisflaeche, die man kennen muss, weil ein falscher
Parser hier still das Falsche liest:

* Der Lauf-Strom setzt **keine** `event:`-Zeile. Es kommt nur `data: {json}`,
  und die Art des Ereignisses steht im JSON unter `event`. Ein Parser, der auf
  `event:` wartet, sieht nie eines.
* Lebenszeichen sind SSE-Kommentare (`: keepalive`). Sie sind keine Daten und
  duerfen nicht als solche gelesen werden.

Fehler werden hier uebersetzt, nicht durchgereicht. Eine rohe Anbieter-Meldung
ist Diagnose, kein Produktvertrag: sie wandert ins Protokoll, waehrend nach oben
ein stabiler Grund geht — `provider_quota`, `task_budget_exhausted`,
`provider_auth`, `executor_unavailable`, `executor_failure`.
"""
from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from solvio.logging_setup import get_logger

log = get_logger("deep")

#: Werkzeuge, die dieser Executor in V1 ueberhaupt haben darf. Wird beim Start
#: gegen die Selbstauskunft von Hermes geprueft — Vertrauen ist nett, eine
#: Behauptung des Prozesses selbst ist besser, und beides zusammen ist genug.
ALLOWED_TOOLSETS = frozenset({"web"})

#: Was Hermes ueber sich sagt, wenn ein Werkzeugaufruf Autoritaet braeuchte.
APPROVAL_EVENT = "approval.request"

_QUOTA = re.compile(r"(?i)quota|rate.?limit|insufficient_quota|429|billing|credit")
_AUTH = re.compile(r"(?i)401|403|unauthor|authentication|invalid[_ ]api[_ ]key|missing auth")

#: Die Codes, die der eigene Broker in seiner kategorischen Absage schickt
#: (`{"error":{"type":"solvio_broker","code":…}}`). Sie stehen woertlich im
#: Fehlertext des Laufs — gemessen im Journal.
#:
#: **Sie werden VOR den Regexen gelesen, und das ist tragend.** Jede
#: Broker-Absage traegt auch ihren HTTP-Status im Text, und `_QUOTA` matcht
#: „429". Ohne diese Reihenfolge wuerde eine erschoepfte AUFGABENgrenze als
#: Tageskontingent gemeldet — „das Budget erholt sich in der Nacht", obwohl
#: sich das Budget dieser Aufgabe nie erholt. Der Mensch wartet dann auf eine
#: Nacht, die nichts aendert.
_BROKER_TASK_BUDGET = ("lease_budget_exhausted", "lease_request_capped",
                       "duplicate_request")
_BROKER_QUOTA = ("token_capped", "rate_capped")


class HermesError(RuntimeError):
    """Ein Fehlschlag mit einem stabilen Grund — nie mit der rohen Meldung."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail[:200]


def classify(message: str) -> str:
    """Uebersetzt eine Anbieter-Meldung in einen der stabilen Gruende.

    Reihenfolge: die eigenen Broker-Codes zuerst, danach die allgemeinen
    Muster. Umgekehrt gewinnt der HTTP-Status im Text ueber den Code, und die
    Unterscheidung „Tag erschoepft" / „diese Aufgabe erschoepft" faellt weg.
    """
    text = message or ""
    if any(code in text for code in _BROKER_TASK_BUDGET):
        return "task_budget_exhausted"
    if any(code in text for code in _BROKER_QUOTA):
        return "provider_quota"
    if _QUOTA.search(text):
        return "provider_quota"
    if _AUTH.search(text):
        return "provider_auth"
    return "executor_failure"


class HermesClient:
    """Sechs Aufrufe. Mehr Flaeche bekommt SOLVIO von Hermes nicht."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _json(self, method: str, path: str,
                    body: dict[str, Any] | None = None) -> Any:
        session = await self._client()
        try:
            async with session.request(method, self.base_url + path, json=body) as reply:
                text = await reply.text()
                if reply.status >= 400:
                    raise HermesError(
                        "provider_auth" if reply.status in (401, 403) else "executor_failure",
                        f"HTTP {reply.status}: {text[:160]}")
                return json.loads(text) if text else {}
        except aiohttp.ClientError as exc:
            raise HermesError("executor_unavailable", f"{type(exc).__name__}") from exc
        except TimeoutError as exc:
            raise HermesError("executor_unavailable", "timeout") from exc

    # -- Zustand des Executors ----------------------------------------------
    async def healthy(self) -> bool:
        try:
            reply = await self._json("GET", "/health")
        except HermesError:
            return False
        return isinstance(reply, dict) and reply.get("status") == "ok"

    async def enabled_toolsets(self) -> list[str]:
        """Was der Executor nach eigener Auskunft wirklich anbieten kann."""
        reply = await self._json("GET", "/v1/toolsets")
        entries = reply.get("data", []) if isinstance(reply, dict) else []
        return sorted(str(e.get("name", "")) for e in entries
                      if isinstance(e, dict) and e.get("enabled"))

    async def surface_violations(self) -> list[str]:
        """Werkzeuggruppen jenseits des Erlaubten. Leer heisst: Haltung stimmt."""
        return [name for name in await self.enabled_toolsets()
                if name not in ALLOWED_TOOLSETS]

    # -- Laeufe --------------------------------------------------------------
    async def submit(self, *, instruction: str, model: str, provider: str,
                     system: str = "") -> str:
        body: dict[str, Any] = {"input": instruction, "model": model,
                                "provider": provider}
        if system:
            body["instructions"] = system
        reply = await self._json("POST", "/v1/runs", body)
        run_id = str(reply.get("run_id", "")) if isinstance(reply, dict) else ""
        if not run_id:
            raise HermesError("executor_failure", "no run id in submit reply")
        return run_id

    async def status(self, run_id: str) -> dict[str, Any]:
        reply = await self._json("GET", f"/v1/runs/{run_id}")
        return reply if isinstance(reply, dict) else {}

    async def stop(self, run_id: str) -> bool:
        """Bittet den fernen Lauf aufzuhoeren. Fehlschlag ist kein Drama.

        Der Abbruch gilt bei SOLVIO schon, bevor dieser Aufruf ueberhaupt
        stattfindet — deshalb darf er scheitern, ohne dass jemand weiterlaeuft.
        """
        try:
            await self._json("POST", f"/v1/runs/{run_id}/stop", {})
            return True
        except HermesError as exc:
            log.info("deep.remote_stop_failed", reason=exc.reason)
            return False

    async def answer_approval(self, run_id: str, *, allow: bool) -> bool:
        """Beantwortet eine Nachfrage des Executors. In V1 immer ablehnend.

        Der Wert liegt nicht in der Antwort, sondern darin, wer sie gibt: die
        Entscheidung faellt bei SOLVIO. Hermes fragt, SOLVIO antwortet.
        """
        try:
            await self._json("POST", f"/v1/runs/{run_id}/approval",
                             {"choice": "once" if allow else "deny", "all": False})
            return True
        except HermesError as exc:
            log.info("deep.approval_answer_failed", reason=exc.reason)
            return False

    async def events(self, run_id: str):
        """Der Ereignisstrom eines Laufs, Stueck fuer Stueck.

        Nur `data:`-Zeilen sind Daten. Kommentare (`: keepalive`) werden
        verworfen; kaputte Zeilen ebenfalls, statt den Strom zu beenden — eine
        unlesbare Zeile ist kein Grund, eine laufende Aufgabe zu verlieren.
        """
        session = await self._client()
        url = f"{self.base_url}/v1/runs/{run_id}/events"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=None)) as reply:
                if reply.status >= 400:
                    raise HermesError("executor_failure", f"HTTP {reply.status}")
                async for raw in reply.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except ValueError:
                        log.info("deep.unparsable_event")
                        continue
                    if isinstance(payload, dict):
                        yield payload
        except aiohttp.ClientError as exc:
            raise HermesError("executor_unavailable", type(exc).__name__) from exc
