"""SOLVIO besitzt die tiefe Aufgabe. Hermes fuehrt sie aus.

Das ist der ganze Satz, und jede Zeile hier dient ihm. Konkret heisst er:

**Identitaet.** Die Aufgabe heisst `dt-…` und diesen Namen vergibt SOLVIO, bevor
irgendetwas nach aussen geht. Was Hermes zurueckmeldet, ist ein Griff — gut zum
Stoppen, zum Nachfragen, zum Wiederanknuepfen — und niemals der Name der
Aufgabe. Ein Executor, der neu startet und seine Kennungen vergisst, verliert
damit nichts, was SOLVIO gehoert.

**Abbruch.** Er gilt zuerst hier und dann dort. Der Journaleintrag steht, bevor
das erste Paket den Rechner verlaesst; das ferne Stopp ist ein Versuch, keine
Bedingung. Deshalb kann ein nicht erreichbarer Executor nie der Grund sein, dass
etwas weiterlaeuft — und deshalb kann verspaetete Ausgabe eine abgebrochene
Aufgabe nicht wiederbeleben.

**Zeit und Budget.** Die Frist gehoert SOLVIO. Ein Executor, der mehr Zeit oder
mehr Token moechte, meldet Verbrauch; er verhandelt nicht. Laeuft die Frist ab,
wird abgebrochen — nicht verlaengert.

**Ergebnis.** Was zurueckkommt, ist Information. Es wird entwaffnet, mit seiner
Herkunft beschriftet und in ein Datenfeld gelegt. Verlangt die Aufgabe eine
Form, prueft SOLVIO sie selbst und gibt genau **einen** Korrekturversuch. Danach
sagt das Ergebnis die Wahrheit, statt in einer Reparaturschleife zu verschwinden.

**Freigabe.** Fragt Hermes nach Autoritaet, antwortet SOLVIO. In dieser Stufe
lautet die Antwort immer Nein, und das ist kein Platzhalter: die erlaubte
Werkzeugflaeche ist rein lesend, also kann eine noetige Freigabe nur bedeuten,
dass etwas nicht stimmt. Der Weg zum iPhone existiert bereits — er wird
betreten, wenn eine tiefe Aufgabe je etwas wirken darf, und nicht vorher.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from typing import Any

from solvio.contracts.deep_runtime import (
    DeepTask, DeepTaskHandle, DeepTaskResult, DeepTaskStatus, Source, TaskCost,
)
from solvio.contracts.trust import TrustLevel
from solvio.deep import executor as ex
from solvio.deep.events import CONTENT_TRUST, DeepEvent, EventKind, as_information, neutralize
from solvio.deep.hermes import HermesClient, HermesError, classify
from solvio.deep.journal import (
    CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, TERMINAL, TIMED_OUT,
    WAITING_FOR_USER, DeepJournal,
)
from solvio.logging_setup import get_logger
from solvio.provider_broker.service import DEEP_PRINCIPAL as BROKER_PRINCIPAL

log = get_logger("deep")

#: Wie viele PHYSISCHE Anbieteraufrufe eine Aufgabe machen darf — die zweite
#: Haelfte des Aufgabenbudgets, neben `task.budget.max_tokens`.
#:
#: Sie steht hier und nicht in der Aufgabe, weil sie eine Eigenschaft dieses
#: EXECUTORS ist und keine des Auftrags: gemessen macht eine vollstaendige
#: Recherche zwoelf Weiterleitungen, ein Schema-Korrekturlauf verdoppelt das
#: schlimmstenfalls. 40 laesst Luft und reisst trotzdem sofort, wenn jemand
#: dieselbe Anfrage in einer Schleife schickt.
MAX_PROVIDER_CALLS = 40

#: Wie oft eine formwidrige Antwort korrigiert werden darf. Genau einmal.
#: Ein Modell, das die Form beim zweiten Anlauf nicht trifft, trifft sie auch
#: beim zwanzigsten nicht — es verbrennt nur Geld und Zeit dabei.
MAX_SCHEMA_RETRIES = 1

#: Die Anweisung, die SOLVIO dem Executor voranstellt. Sie ist keine Sicherheit
#: — Sicherheit ist die Werkzeugflaeche und die Sandbox. Sie ist Hoeflichkeit
#: gegenueber dem Modell, damit brauchbare Recherche herauskommt.
SYSTEM_INSTRUCTION = (
    "Du recherchierst fuer SOLVIO. Arbeite mit oeffentlich zugaenglichen Quellen. "
    "Nenne fuer jede wesentliche Aussage die Quelle als URL. Benenne offene Fragen "
    "ausdruecklich, statt sie zu ueberdecken. Halte nach jeder Suchrunde die "
    "auftragsrelevanten Befunde mit zugehoeriger URL, Datum und Einschraenkungen "
    "knapp fest, damit sie bei Kontextverdichtung erhalten bleiben. Suche nicht "
    "wiederholt dieselben unergiebigen Treffer. Wenn eine konkrete Angabe wie "
    "ein tagesaktueller Preis nicht oeffentlich belegbar ist, nenne genau diese "
    "Luecke samt geprueften Quellen im Ergebnis. Antworte auf Deutsch."
)

#: Zustaende, nach denen kein Ergebnis mehr veraendert wird.
_DONE = TERMINAL

_JSON_TYPES = {"string": str, "number": (int, float), "integer": int,
               "boolean": bool, "object": dict, "array": list}

_STATUS_MAP = {
    QUEUED: DeepTaskStatus.QUEUED, RUNNING: DeepTaskStatus.RUNNING,
    WAITING_FOR_USER: DeepTaskStatus.WAITING_FOR_USER,
    SUCCEEDED: DeepTaskStatus.SUCCEEDED, FAILED: DeepTaskStatus.FAILED,
    CANCELLED: DeepTaskStatus.CANCELLED, TIMED_OUT: DeepTaskStatus.TIMED_OUT,
}

#: Uebersetzung der Executor-Sprache in SOLVIOs elf Zustaende. Was hier fehlt,
#: wird Beobachtung — ein unbekanntes Ereignis ist Information, kein Zustand.
#:
#: Auffaellig fehlen `run.completed`, `run.failed` und `run.cancelled`, und das ist
#: der Kern der Eigentumsfrage: der Executor beendet einen **Lauf**, nicht die
#: **Aufgabe**. Wuerde sein Fertig hier direkt zum Endzustand, waere die Aufgabe
#: abgeschlossen, bevor SOLVIO die Form des Ergebnisses ueberhaupt gesehen hat —
#: und ein Korrekturlauf traefe auf eine bereits geschlossene Aufgabe. Das Ende
#: setzt ausschliesslich `_succeed` bzw. `_fail`, nach der Pruefung.
_EVENT_MAP = {
    "run.started": EventKind.RUNNING,
    "run.stopping": EventKind.CANCELLING,
    "tool.started": EventKind.TOOL_REQUESTED,
    "approval.request": EventKind.WAITING_FOR_APPROVAL,
    "approval.responded": EventKind.RESUMED,
}


def schema_errors(value: Any, schema: dict[str, Any] | None) -> str:
    """Prueft ein Ergebnis gegen die gewuenschte Form. Leer heisst: passt.

    Absichtlich klein — kein JSON-Schema-Vollausbau. Geprueft wird, was eine
    Aufgabe wirklich zusagt: Typ, Pflichtfelder, Feldtypen. Alles darueber
    hinaus waere ein Framework, das niemand bestellt hat.
    """
    if not schema:
        return ""
    if schema.get("type") == "object" and not isinstance(value, dict):
        return "not_an_object"
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                return f"missing_field:{name}"
        for name, rule in (schema.get("properties") or {}).items():
            if name not in value:
                continue
            expected = _JSON_TYPES.get(str(rule.get("type", "")))
            if expected is not None and not isinstance(value[name], expected):
                return f"wrong_type:{name}"
    return ""


class HermesDeepRuntime:
    """Ein DeepRuntime, dessen Autoritaet vollstaendig bei SOLVIO liegt."""

    def __init__(self, *, journal: DeepJournal, client: HermesClient,
                 config: ex.ExecutorConfig, process: Any = None,
                 broker: Any = None) -> None:
        self.journal = journal
        self.client = client
        self.config = config
        self.process = process
        self.broker = broker
        self._pumps: dict[str, asyncio.Task] = {}
        self._results: dict[str, DeepTaskResult] = {}
        self._leases: dict[str, str] = {}
        #: Der gemeldete Verbrauch je Aufgabe, aufsummiert ueber alle Laeufe
        #: (der Schema-Korrekturlauf ist ein zweiter). Er stand bisher im
        #: `run.completed`-Ereignis auf dem Draht und wurde zweimal
        #: weggeworfen: `_one_run` las nur `output`, und die Whitelist in
        #: `_payload` liess ihn nicht durch. `TaskCost()` blieb dadurch
        #: konstant null — eine Kostenrechnung, die nie etwas kostete.
        self._cost: dict[str, TaskCost] = {}

    # -- Der Not-Aus (§14) ---------------------------------------------------
    async def paused(self) -> bool:
        return await self.journal.paused()

    async def pause(self, *, cancel_running: bool = True) -> int:
        """Haelt neue tiefe Ausfuehrung an — und raeumt auf Wunsch die laufende ab.

        Voreinstellung ist Abbrechen, weil ein Not-Aus, der Laufendes laufen
        laesst, keiner ist. Der Zustand liegt in der Datenbank; ein Neustart hebt
        ihn also nicht versehentlich auf.
        """
        await self.journal.set_paused(True)
        stopped = 0
        if cancel_running:
            for task in await self.journal.unfinished():
                if await self.cancel_task(task["task_id"]):
                    stopped += 1
        log.info("deep.paused", cancelled=stopped)
        return stopped

    async def unpause(self) -> None:
        await self.journal.set_paused(False)
        log.info("deep.unpaused")

    # -- Der Contract --------------------------------------------------------
    async def run_task(self, task: DeepTask) -> DeepTaskHandle:
        """Nimmt eine Aufgabe an — oder sagt sofort, warum nicht."""
        if await self.journal.paused():
            raise DeepPaused("deep_paused")
        await self.journal.create(task.id, task.task_type.value, task.instruction)
        # Das Lease geht als LETZTE Anweisung vor der Pumpe auf: nach der
        # Pausenpruefung und nach `journal.create`. Eine frueher geoeffnete
        # Leihe ueberlebte jeden Fruehausstieg, ohne dass je eine Pumpe
        # existierte, die sie wieder schloesse.
        self._open_lease(task)
        self._pumps[task.id] = asyncio.create_task(self._drive(task))
        return DeepTaskHandle(id=task.id, status=DeepTaskStatus.QUEUED,
                              task_type=task.task_type)

    async def get_status(self, id: str) -> DeepTaskStatus:
        row = await self.journal.task(id)
        if row is None:
            raise KeyError(id)
        return _STATUS_MAP[row["status"]]

    async def get_result(self, id: str) -> DeepTaskResult | None:
        row = await self.journal.task(id)
        if row is None or row["status"] not in (SUCCEEDED, FAILED, CANCELLED, TIMED_OUT):
            return None
        if id in self._results:
            return self._results[id]
        data = json.loads(row["result_json"]) if row["result_json"] else None
        return DeepTaskResult(success=row["status"] == SUCCEEDED, data=data,
                              errors=[row["failure_reason"]] if row["failure_reason"] else [])

    async def cancel_task(self, id: str) -> bool:
        """Abbruch — lokal verbindlich, fern nur versucht (§13).

        Die Pumpe wird ausdruecklich NICHT abgeschossen, und das ist der
        interessante Teil. Ein Abbruch mitten in der Einreichung wuerde sonst
        genau dann treffen, wenn die Anfrage schon unterwegs ist: der ferne Lauf
        entstuende, und SOLVIO erfuehre seine Kennung nie — ein Waisenlauf, der
        unbeaufsichtigt weiterarbeitet. Also darf die Pumpe zu Ende einreichen.
        Sie erfaehrt beim naechsten Journalschreiben, dass die Aufgabe nicht mehr
        ihr gehoert, und schickt dem fernen Lauf selbst ein Stopp hinterher.
        """
        cancelled = await self.journal.cancel(id)
        run_id = await self.journal.run_id(id)
        if run_id:
            await self.client.stop(run_id)
        return cancelled

    async def resume_task(self, id: str) -> bool:
        row = await self.journal.task(id)
        return bool(row) and row["status"] == WAITING_FOR_USER

    async def list_tasks(self, *, status: DeepTaskStatus | None = None,
                         origin: Any = None) -> list[DeepTaskHandle]:
        rows = await self.journal.tasks()
        handles = []
        for row in rows:
            mapped = _STATUS_MAP[row["status"]]
            if status is not None and mapped is not status:
                continue
            from solvio.contracts.deep_runtime import DeepTaskType
            handles.append(DeepTaskHandle(id=row["task_id"], status=mapped,
                                          task_type=DeepTaskType(row["task_type"])))
        return handles

    async def health(self) -> dict[str, Any]:
        unfinished = await self.journal.unfinished()
        return {
            "executor": "hermes",
            "reachable": await self.client.healthy(),
            "sandboxed": self.process is not None,
            "paused": await self.journal.paused(),
            "active": len(unfinished),
        }

    # -- Mitlesen (§11/§12) --------------------------------------------------
    def stream(self, task_id: str, *, after_seq: int = -1):
        """Erst nachholen, dann mitlesen. Ohne Loch an der Nahtstelle."""
        return self.journal.stream(task_id, after_seq=after_seq)

    # -- Die Maschine --------------------------------------------------------
    async def _drive(self, task: DeepTask) -> None:
        """Fuehrt eine Aufgabe von Anfang bis Ende. Faellt nie ungeklaert um.

        Das `finally` ist der einzige Ort, der **jeden** Endzustand sieht: den
        Erfolg, den Schemafehler, den Zeitablauf, den Abbruch, den Absturz —
        und den Selbstabbruch, bei dem `_one_run` schlicht `None` liefert und
        `_drive` ohne jede Ausnahme durchlaeuft.
        """
        try:
            await asyncio.wait_for(self._attempt(task), timeout=task.timeout)
        except (TimeoutError, asyncio.TimeoutError):
            # Die Frist gehoert SOLVIO. Abgelaufen heisst abbrechen, nicht warten.
            await self._stop_remote(task.id)
            await self._fail(task.id, "timeout", status=TIMED_OUT)
        except asyncio.CancelledError:
            # Hier NICHT stoppen: in einer abgebrochenen Koroutine wirft der
            # naechste `await` sofort wieder. Der Fall ist ohnehin gedeckt —
            # `cancel_task` schickt das Stopp selbst, und ein Core-Neustart
            # beendet jede unfertige Aufgabe ueber denselben Weg
            # (`service.py`, `journal.unfinished()`).
            raise
        except HermesError as exc:
            log.info("deep.executor_error", reason=exc.reason, detail=exc.detail)
            await self._stop_remote(task.id)
            await self._fail(task.id, exc.reason)
        except Exception as exc:  # noqa: BLE001 - eine tiefe Aufgabe crasht den Core nicht
            log.error("deep.driver_failed", kind=type(exc).__name__)
            await self._stop_remote(task.id)
            await self._fail(task.id, "executor_failure")
        finally:
            self._close_lease(task.id)

    async def _stop_remote(self, task_id: str) -> None:
        """Bittet den fernen Lauf aufzuhoeren — auf JEDEM Fehlerpfad.

        Vorher stand das nur im Zeitablauf-Zweig. Riss der Ereignisstrom ab
        (`HermesError`) oder stolperte die Pumpe, endete die Aufgabe bei SOLVIO
        — und der Lauf im Kaefig arbeitete weiter. Er hielt dabei den
        rotierten Broker-Token in der Hand und hämmerte als `bad_token` gegen
        den Broker: 163 solche Zeilen stehen im Buch.

        Ein Fehlschlag des Stopps ist kein Drama und wird nie zur Bedingung:
        die Aufgabe ist bei SOLVIO schon beendet, wenn dieser Aufruf laeuft.
        """
        try:
            run_id = await self.journal.run_id(task_id)
            if run_id:
                await self.client.stop(run_id)
        except Exception as exc:  # noqa: BLE001 - ein Aufraeumen wirft nie nach oben
            log.info("deep.remote_stop_skipped", kind=type(exc).__name__)

    # -- Das Zeitfenster beim Broker -----------------------------------------
    def _open_lease(self, task: DeepTask) -> None:
        """Oeffnet das Zeitfenster fuer genau diese Aufgabe — samt Budget.

        Die Frist ist ein **Rueckfallnetz gegen vergessene Leases**, nicht die
        eigentliche Schranke — die ist der Schluss im `finally`. Sie liegt
        bewusst ueber der Aufgabenfrist, damit ein Lauf nicht mitten in der
        Verdichtung sein eigenes Fenster verliert.

        **Das Budget der Aufgabe reist hier hinueber, und erst dadurch
        existiert es.** `task.budget.max_tokens` stand bis zu diesem Milestone
        in jeder Aufgabe und wurde nirgends gelesen. Der Broker setzt es jetzt
        durch — VOR seiner Tageskappe, damit eine einzelne Aufgabe den Tag
        nicht leerfressen kann. SOLVIO setzt es; der Kaefig kann es weder
        ausstellen noch anheben.
        """
        if self.broker is None:
            return
        budget = getattr(task, "budget", None)
        max_tokens = getattr(budget, "max_tokens", None) if budget else None
        try:
            self._leases[task.id] = self.broker.open_lease(
                BROKER_PRINCIPAL, task.id, deadline=time.time() + task.timeout + 120,
                max_tokens=max_tokens, max_requests=MAX_PROVIDER_CALLS)
        except Exception as exc:  # noqa: BLE001
            # Kein Lease heisst: die Aufgabe laeuft und bekommt vom Broker
            # `403`. Das ist ehrlicher als ein Lauf, der still ohne Fenster
            # arbeitet — und es steht im Buch.
            log.error("deep.lease_open_failed", kind=type(exc).__name__)

    def _close_lease(self, task_id: str) -> None:
        lease = self._leases.pop(task_id, "")
        if lease and self.broker is not None:
            self.broker.close_lease(lease)

    async def _attempt(self, task: DeepTask) -> None:
        instruction = task.instruction
        for attempt in range(MAX_SCHEMA_RETRIES + 1):
            output = await self._one_run(task, instruction)
            if output is None:
                return                      # abgebrochen oder bereits gescheitert
            parsed, problem = _shape(output, task.output_schema)
            if not problem:
                await self._succeed(task, parsed, output)
                return
            if attempt >= MAX_SCHEMA_RETRIES:
                log.info("deep.schema_unmet", problem=problem)
                await self._fail(task.id, f"schema_invalid:{problem}")
                return
            instruction = (
                f"{task.instruction}\n\nDeine vorige Antwort hielt die verlangte Form "
                f"nicht ein ({problem}). Antworte ausschliesslich mit gueltigem JSON "
                f"nach diesem Schema: {json.dumps(task.output_schema, ensure_ascii=False)}")
            await self.journal.record(task.id, EventKind.OBSERVATION,
                                      {"schema_retry": problem})

    async def _one_run(self, task: DeepTask, instruction: str) -> str | None:
        await self.journal.record(task.id, EventKind.EXECUTOR_STARTING,
                                  {"executor": "hermes", "model": self.config.model})
        run_id = await self.client.submit(instruction=instruction,
                                          model=self.config.model,
                                          provider=self.config.provider,
                                          system=SYSTEM_INSTRUCTION)
        still_wanted = await self.journal.attach_run(task.id, run_id)
        if not still_wanted:
            # Waehrend der Einreichung wurde abgebrochen. Der ferne Lauf existiert
            # nun — also wird er gestoppt, statt ihn unbeaufsichtigt zu lassen.
            await self.client.stop(run_id)
            return None

        text: list[str] = []
        final = ""
        async for raw in self.client.events(run_id):
            kind = str(raw.get("event", ""))
            if kind == "message.delta":
                text.append(str(raw.get("delta", "")))
                continue
            mapped = _EVENT_MAP.get(kind, EventKind.OBSERVATION)
            recorded = await self.journal.record(task.id, mapped, _payload(kind, raw))
            if recorded is None:
                return None                 # Aufgabe ist fort; nichts mehr aendern
            if kind == "approval.request":
                await self._decide_approval(task, run_id, raw)
                continue
            if kind == "run.completed":
                self._note_usage(task.id, raw.get("usage"))
                final = str(raw.get("output", "")) or "".join(text)
                break
            if kind == "run.failed":
                await self._fail(task.id, classify(str(raw.get("error", ""))))
                return None
            if kind == "run.cancelled":
                # Der Executor hat von sich aus aufgehoert. Hat SOLVIO das nicht
                # angeordnet, muss die Aufgabe trotzdem ein Ende bekommen —
                # sonst stuende sie fuer immer auf „laeuft".
                await self.journal.finish(task.id, EventKind.CANCELLED,
                                          reason="cancelled_by_executor")
                return None
        return final or "".join(text)

    def _note_usage(self, task_id: str, usage: Any) -> None:
        """Nimmt den gemeldeten Verbrauch eines Laufs an. Nur Zahlen.

        Aus dem Ereignis wird ausschliesslich Gezaehltes gelesen und in `int`
        gezwungen — kein Text, keine verschachtelte Struktur, nichts, was ein
        Executor als Traeger fuer Inhalt benutzen koennte.
        """
        if not isinstance(usage, dict):
            return
        try:
            tokens = int(usage.get("input_tokens", 0) or 0) + \
                int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            return
        if tokens <= 0:
            return
        current = self._cost.get(task_id, TaskCost())
        self._cost[task_id] = TaskCost(tokens=current.tokens + tokens,
                                       cost_usd=current.cost_usd)

    async def _decide_approval(self, task: DeepTask, run_id: str,
                               raw: dict[str, Any]) -> None:
        """Hermes fragt. SOLVIO antwortet. Immer SOLVIO.

        In V1 ist die Antwort Nein, weil die erlaubte Flaeche rein lesend ist:
        eine noetige Freigabe bedeutet hier, dass etwas nicht stimmt. Die
        Entscheidung faellt trotzdem hier und nicht dort — das ist der Punkt.
        """
        log.info("deep.approval_requested", task_id=task.id,
                 command=neutralize(raw.get("command", ""), limit=120))
        await self.client.answer_approval(run_id, allow=False)
        await self.journal.record(task.id, EventKind.RESUMED,
                                  {"decision": "denied", "by": "solvio"})

    async def _succeed(self, task: DeepTask, parsed: Any, output: str) -> None:
        sources = [Source(ref=url, trust_level=TrustLevel.UNTRUSTED_WEB)
                   for url in _urls(output)]
        cost = self._cost.pop(task.id, TaskCost())
        result = DeepTaskResult(
            success=True,
            summary=neutralize(output, limit=600),
            data=parsed if parsed is not None else as_information(output),
            sources=sources,
            cost=cost)
        self._results[task.id] = result
        log.info("deep.task_cost", task_id=task.id, tokens=cost.tokens,
                 budget=getattr(getattr(task, "budget", None), "max_tokens", None))
        await self.journal.finish(task.id, EventKind.SUCCEEDED,
                                  result={"content_trust": CONTENT_TRUST,
                                          "data": parsed if parsed is not None
                                          else neutralize(output),
                                          "sources": [s.ref for s in sources],
                                          # Gezaehltes, kein Text. Es beantwortet
                                          # im Nachhinein die Frage, was eine
                                          # Aufgabe wirklich gekostet hat.
                                          "tokens": cost.tokens})

    async def _fail(self, task_id: str, reason: str, *, status: str = "") -> None:
        """Beendet eine Aufgabe mit Grund — sofern sie nicht schon beendet ist.

        Die Pruefung ist dieselbe Regel wie im Journal, nur eine Ebene hoeher:
        auch der Zwischenspeicher darf ein fertiges Ergebnis nicht nachtraeglich
        in einen Fehlschlag verwandeln. Ohne sie machte ein Stolperer NACH der
        Fertigstellung aus einer gelungenen Recherche eine gescheiterte.
        """
        row = await self.journal.task(task_id)
        if row is not None and row["status"] in _DONE:
            log.info("deep.late_failure_ignored", reason=reason)
            return
        await self.journal.finish(task_id, EventKind.FAILED, reason=reason,
                                  status=status)
        self._results[task_id] = DeepTaskResult(
            success=False, errors=[reason], cost=self._cost.pop(task_id, TaskCost()))


class DeepPaused(RuntimeError):
    """Tiefe Ausfuehrung ist angehalten. Nichts wurde begonnen."""


def _payload(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Was von einem Executor-Ereignis ins Journal darf — entwaffnet.

    Nie die rohe Nutzlast: sie kann beliebigen Text aus einer Webseite tragen,
    und das Journal wird spaeter gelesen, angezeigt und in Prompts einbezogen.
    """
    payload: dict[str, Any] = {"executor_event": kind}
    for field in ("tool", "name", "status", "error", "command", "text", "delta"):
        if field in raw:
            payload[field] = neutralize(raw[field], limit=400)
    payload["content_trust"] = CONTENT_TRUST
    return payload


def _shape(output: str, schema: dict[str, Any] | None) -> tuple[Any, str]:
    """Bringt eine Antwort in die verlangte Form — oder benennt das Problem."""
    if not schema:
        return None, ""
    text = output.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, "not_json"
    problem = schema_errors(parsed, schema)
    return (parsed, problem) if not problem else (None, problem)


_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _urls(text: str) -> list[str]:
    """Sammelt die genannten Quellen. Sie bleiben Verweise, nie Autoritaet."""
    seen: list[str] = []
    for match in _URL.findall(text or ""):
        cleaned = match.rstrip(".,;:")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen[:12]


def new_task_id() -> str:
    """Die SOLVIO-Kennung. Entsteht hier, nicht beim Executor."""
    return "dt-" + secrets.token_hex(8)
