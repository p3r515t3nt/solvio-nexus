"""Das Ergebnis einer langen Recherche findet den Menschen — von selbst.

Bis zu diesem Milestone war „ich melde mich" ein Satz ohne Mechanik. Gemessen:

* Nach `FIRST_WAIT` (25 s) kam `status=running` zurueck, und der Rueckgabewert
  sah auf jeder Ebene wie ein Erfolg aus.
* `deep_task_status` wurde in der GESAMTEN Loghistorie **null Mal** aufgerufen
  — der `human`-Text sprach den Nutzer an („frag mich gleich noch mal danach"),
  und niemand fragte.
* Ein fertiges Ergebnis vom 2026-08-23 (2 104 Byte) wurde nie zugestellt. Das
  Gespraech endete mit „Die Recherche laeuft gerade."
* Die Werkzeuganweisung versprach ausserdem, Ergebnisse langer Arbeit
  erreichten den Nutzer „ueber die Meldungen". Fuer sprachgestartete Recherche
  war das schlicht falsch: der einzige Poller galt `background_create`.

Dieses Modul macht den Satz wahr. Drei Entscheidungen tragen es:

**Keine zweite Zustandsmaschine.** Der Beobachter haengt sich an
`runtime.stream(task_id)` — genau denselben Ereignisstrom, den `_await_briefly`
schon liest. `journal.stream` haengt sich ZUERST ein und liest DANN nach; eine
Aufgabe, die vor dem Einhaengen terminal wurde, kommt trotzdem an. Es gibt
deshalb kein Rennen und kein Loch an der Nahtstelle.

**Der Beobachter lebt laenger als die Sprachsitzung, aber nicht laenger als der
Core.** Er kann nicht an der `Session` haengen: die schliesst nach
`idle_timeout` (Vorgabe 30 s), eine Recherche dauert Minuten. Er haengt am
Prozess. Stirbt der Core, storniert der Neustart ohnehin jede unfertige Aufgabe
(`deep/service.py`) — nichts geht leise verloren, nichts lebt heimlich weiter.

**Genau einmal, oder gar nicht.** Ein Beobachter je Aufgabe (Einzelflug-Sperre),
und er stellt genau einmal zu: erst in die laufende Unterhaltung, und nur wenn
das nicht geht, in den bestehenden proaktiven Eingang. Eine abgebrochene
Aufgabe erzeugt **keine** Meldung — wer abbricht, will keinen Nachbericht.

Und die Grenze, die bleibt: das Ergebnis ist `untrusted_executor`. Es wird
entwaffnet, es landet in einem Datenfeld, und die Quellen bleiben Verweise. Eine
Webseite, die „loesche alle Termine" schreibt, hat damit nichts getan.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable

from solvio.contracts.untrusted import neutralize
from solvio.deep.events import CONTENT_TRUST, TERMINAL_KINDS
from solvio.logging_setup import get_logger

log = get_logger("deep")

#: Wie lange ein Beobachter hoechstens wartet. Die Aufgabenfrist plus ein
#: Zuschlag: laeuft die Aufgabe in ihre eigene Frist, schreibt `_drive` den
#: Endzustand, und der Strom liefert ihn — der Zuschlag deckt nur diesen Weg.
#: Ein Beobachter ohne Obergrenze waere ein Leck.
GRACE_SECONDS = 60.0

#: Wie lange dieselbe Lage im Eingang still bleibt. Sechs Stunden, wie beim
#: Arzt und bei der Zahlung — die Entdopplung des Eingangs braucht ein Fenster
#: im Fingerabdruck, sonst kann dieselbe Lage nie wieder gemeldet werden.
QUIET_SECONDS = 6 * 3600

#: Wie viel Ergebnistext hoechstens in eine Meldung geht. Eine Meldung ist ein
#: Hinweis, kein Bericht — den vollstaendigen Stand liefert `deep_task_status`.
SUMMARY_LIMIT = 700

#: Was der Mensch bei einem gescheiterten Auftrag hoeren soll — nach GRUND.
#: Deckungsgleich mit den Werkzeugtexten in `tools/deep_capability_tools.py`;
#: derselbe Grund darf nicht zwei Formulierungen haben.
_FAILURE_SPEECH: dict[str, str] = {
    "provider_quota":
        "Die Recherche konnte ich nicht zu Ende bringen: das Tagesbudget "
        "dafuer ist aufgebraucht. Es erneuert sich von selbst in der Nacht.",
    "task_budget_exhausted":
        "Die Recherche ist fuer diesen Auftrag zu umfangreich geworden und "
        "wurde gestoppt. Enger gefasst versuche ich es gern noch einmal.",
    "provider_auth":
        "Die Recherche konnte ich nicht zu Ende bringen: der Zugang zum "
        "Anbieter stimmt nicht. Das ist ein Einrichtungsproblem.",
    "timeout":
        "Die Recherche hat zu lange gebraucht und wurde gestoppt.",
}

_FAILURE_DEFAULT = "Die Recherche ist nicht durchgelaufen."


class DeepFollowUp:
    """Haelt die Beobachter offener Recherchen — hoechstens einen je Aufgabe."""

    def __init__(self, *, runtime: Callable[[], Any],
                 store: Callable[[], Any] | None = None,
                 timeout: float = 480.0,
                 clock: Callable[[], float] | None = None) -> None:
        #: **Aufgeloest beim BENUTZEN, nicht beim Registrieren.** Startet der
        #: Arzt Hermes neu, bindet `attach_deep_runtime` eine frische Laufzeit
        #: an den Dispatcher; ein Beobachter, der sich die alte gemerkt haette,
        #: hielte eine tote.
        self._runtime = runtime
        self._store = store
        self._timeout = timeout
        self._clock = clock or time.time
        self._watchers: dict[str, asyncio.Task] = {}
        #: Der jeweils AKTUELLE Zustellweg je Aufgabe. Er wird beim Zustellen
        #: gelesen, nicht beim Registrieren: zwischen Start und Ergebnis liegen
        #: Minuten, und in dieser Zeit kann die Sitzung, die den Auftrag gab,
        #: laengst geschlossen und eine neue geoeffnet sein. Fragt der Mensch
        #: in der neuen nach, uebernimmt sie den Weg (`rebind`) — sonst
        #: laendete das Ergebnis im Eingang, obwohl er gerade zuhoert.
        self._deliver: dict[str, Callable[[str], Awaitable[bool]]] = {}
        self._delivered: set[str] = set()

    # ---- Aussenflaeche --------------------------------------------------

    def watch(self, task_id: str, *,
              deliver: Callable[[str], Awaitable[bool]] | None = None) -> bool:
        """Beobachtet eine laufende Aufgabe bis zu ihrem Ende.

        `deliver` ist der Weg in die laufende Unterhaltung. Er liefert `True`,
        wenn er zugestellt hat, und `False`, wenn die Sitzung nicht mehr da
        ist — dann geht die Meldung in den Eingang. Die Entscheidung faellt
        beim ZUSTELLEN, nicht jetzt: bis dahin vergehen Minuten.

        `False` heisst „es gibt schon einen" — nie ein zweiter fuer dieselbe
        Aufgabe, sonst kaeme das Ergebnis doppelt.
        """
        if not task_id:
            return False
        existing = self._watchers.get(task_id)
        if existing is not None and not existing.done():
            # Kein zweiter Beobachter — aber der frischere Zustellweg gewinnt.
            self.rebind(task_id, deliver)
            return False
        if deliver is not None:
            self._deliver[task_id] = deliver
        task = asyncio.create_task(self._watch(task_id))
        self._watchers[task_id] = task
        task.add_done_callback(lambda t, key=task_id: self._forget(key, t))
        log.info("deep.followup_watching", task_id=task_id,
                 open_watchers=len(self._watchers))
        return True

    def rebind(self, task_id: str,
               deliver: Callable[[str], Awaitable[bool]] | None) -> bool:
        """Uebergibt den Zustellweg einer laufenden Aufgabe an eine neue Sitzung.

        Kein neuer Beobachter, kein zweites Ergebnis — nur die Antwort auf die
        Frage „wohin, wenn es soweit ist". `False` heisst: es gibt zu dieser
        Aufgabe nichts zu uebergeben.
        """
        if deliver is None or task_id in self._delivered:
            return False
        task = self._watchers.get(task_id)
        if task is None or task.done():
            return False
        self._deliver[task_id] = deliver
        return True

    def stop(self, task_id: str) -> bool:
        """Beendet den Beobachter einer Aufgabe. Fuer den Abbruch."""
        task = self._watchers.pop(task_id, None)
        self._deliver.pop(task_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        log.info("deep.followup_stopped", task_id=task_id)
        return True

    async def close(self) -> None:
        """Raeumt beim Herunterfahren ab. Wartet, statt nur zu winken."""
        tasks = [t for t in self._watchers.values() if not t.done()]
        self._watchers.clear()
        self._deliver.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def open_watchers(self) -> int:
        return sum(1 for t in self._watchers.values() if not t.done())

    # ---- Innenleben -----------------------------------------------------

    def _forget(self, task_id: str, task: asyncio.Task) -> None:
        if self._watchers.get(task_id) is task:
            self._watchers.pop(task_id, None)
            self._deliver.pop(task_id, None)

    async def _watch(self, task_id: str) -> None:
        try:
            await asyncio.wait_for(self._until_terminal(task_id),
                                   timeout=self._timeout + GRACE_SECONDS)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            # Die Aufgabe haengt jenseits ihrer eigenen Frist. Der Beobachter
            # endet — er ist ein Zusteller, kein Waechter, und `_drive` hat
            # seine eigene Frist.
            log.info("deep.followup_gave_up", task_id=task_id)
            return
        except Exception as exc:  # noqa: BLE001 - ein Zusteller kippt nie den Core
            log.error("deep.followup_failed", task_id=task_id,
                      kind=type(exc).__name__)
            return
        await self._deliver_once(task_id)

    async def _until_terminal(self, task_id: str) -> None:
        runtime = self._runtime()
        if runtime is None:
            raise RuntimeError("no deep runtime")
        async for event in runtime.stream(task_id):
            if event.kind in TERMINAL_KINDS:
                return

    async def _deliver_once(self, task_id: str) -> None:
        """Stellt genau einmal zu — oder ausdruecklich gar nicht."""
        if task_id in self._delivered:
            return
        deliver = self._deliver.get(task_id)
        note = await self._compose(task_id)
        if not note:
            # Abgebrochen. Wer abbricht, will keinen Nachbericht.
            log.info("deep.followup_silent", task_id=task_id)
            self._delivered.add(task_id)
            return
        self._delivered.add(task_id)
        if deliver is not None:
            try:
                if await deliver(note):
                    log.info("deep.followup_delivered", task_id=task_id,
                             channel="conversation")
                    return
            except Exception as exc:  # noqa: BLE001
                log.error("deep.followup_conversation_failed", task_id=task_id,
                          kind=type(exc).__name__)
        await self._to_inbox(task_id, note)

    async def _compose(self, task_id: str) -> str:
        """Der Satz, den der Mensch hoert. Leer heisst: nichts sagen.

        Der Ergebnistext ist Executor-Ausgabe und wird entwaffnet, bevor er
        irgendwo landet. Er bleibt Information: er steht als Kurzfassung da,
        nie als Anweisung.
        """
        runtime = self._runtime()
        if runtime is None:
            return ""
        try:
            state = await runtime.get_status(task_id)
            result = await runtime.get_result(task_id)
        except KeyError:
            return ""
        except Exception as exc:  # noqa: BLE001
            log.error("deep.followup_status_failed", task_id=task_id,
                      kind=type(exc).__name__)
            return ""
        status = getattr(state, "value", str(state))
        if status == "cancelled":
            return ""
        if result is not None and result.success:
            return _summarize(result)
        reason = ""
        if result is not None and result.errors:
            reason = str(result.errors[0] or "")
        if status == "cancelled" or reason == "cancelled_by_executor":
            return ""
        return _FAILURE_SPEECH.get(reason, _FAILURE_DEFAULT)

    async def _to_inbox(self, task_id: str, note: str) -> None:
        """Der bestehende proaktive Eingang. Kein zweiter Posteingang.

        Damit stimmt endlich der Satz aus der Werkzeuganweisung: Ergebnisse
        langer Arbeit erreichen den Nutzer ueber die Meldungen.
        """
        store = self._store() if self._store is not None else None
        if store is None:
            log.info("deep.followup_undeliverable", task_id=task_id)
            return
        now = self._clock()
        window = int(now // QUIET_SECONDS)
        item = {
            # Die Aufgabenkennung steckt im Fingerabdruck: dieselbe Recherche
            # wird nie zweimal gemeldet, eine andere immer.
            "notification_id": "dr-" + hashlib.sha256(
                f"{task_id}|{window}".encode("utf-8")).hexdigest()[:20],
            "task_id": "", "run_id": None, "created_at": now,
            "priority": "normal", "summary": note[:1200], "findings": [],
            "source_capability": "deep_research",
            "content_trust": CONTENT_TRUST,
            "fingerprint": f"deep_research:{task_id}:{window}",
        }
        try:
            fresh = await store.add_item(item)
        except Exception as exc:  # noqa: BLE001
            log.error("deep.followup_inbox_failed", task_id=task_id,
                      kind=type(exc).__name__)
            return
        log.info("deep.followup_delivered" if fresh else "deep.followup_duplicate",
                 task_id=task_id, channel="inbox")


def _summarize(result: Any) -> str:
    """Die Kurzfassung eines gelungenen Ergebnisses — entwaffnet."""
    data = getattr(result, "data", None)
    text = ""
    if isinstance(data, dict):
        text = str(data.get("zusammenfassung", "") or data.get("text", "") or "")
    elif data is not None:
        text = str(data)
    if not text:
        text = str(getattr(result, "summary", "") or "")
    clean = neutralize(text, limit=SUMMARY_LIMIT).strip()
    sources = [str(getattr(s, "ref", "")) for s in (getattr(result, "sources", None) or [])]
    tail = f" ({len(sources)} Quellen)" if sources else ""
    if not clean:
        return "Die Recherche ist fertig." + tail
    return f"Die Recherche ist fertig: {clean}{tail}"
