"""Die Verarbeitung einer Chat-Nachricht (§3 des C3-Vertrags).

DER STORE IST DIE WARTESCHLANGE. Der Prozessor haelt kein Lock-Dict und keine
eigene Liste offener Nachrichten: je Chat hoechstens EIN Worker-Task und ein
`dirty`-Ereignis, und der Worker holt sich seine Arbeit mit einer bedingten
Uebernahme aus `conversation_deliveries` (`accepted → running` unter der
Prozessgeneration dieses Prozessors). Zwei Nachrichten desselben Chats laufen
damit strikt seriell in Annahmereihenfolge; eine Semaphore JE ZUSTELLUNG sorgt
dafuer, dass ein gespraechiger Chat die anderen nicht aushungert.

WAS VOR JEDER WIRKUNG STEHT. `current()` — Core-ID, Quellen-Generation
(Browsersitzung bzw. Geraetestand) und Chat-Eigentum — laeuft zu Beginn jeder
Verarbeitung, als `source_check` unmittelbar vor Reservierung und Claim, und
unmittelbar vor `router.execute` bzw. `admit_followup`. Eine Nachricht, deren
Anmeldung inzwischen verfallen ist, wird `blocked/source_revoked`: kein Task,
kein Followup, keine Antwort.

WAS EIN NEUSTART TUT. Der Dispatch-Entscheid wird NACH dem Routing und VOR jeder
Wirkung persistiert. `recover()` weckt jeden Chat mit offenen Zustellungen; was
mit einer uebernommenen Waise geschieht, entscheidet ausschliesslich ihr
persistierter Zustand — nie eine erneute Klassifikation. Ob ein physischer
Modellaufruf wiederholt werden darf, entscheiden die vorhandenen Kostentore
(Aktivitaet, Reservierung, Claim), nicht dieser Prozessor.

Im normalen Betrieb liegt kein `wait_for` um einen Modellaufruf. Beim Core-Stopp
endet die Auslaufzeit mit Cancellation; der Starter beendet seine Prozessgruppe,
und das bestehende Kostentor haelt einen begonnenen Claim als ungewiss fest.

LOGS TRAGEN KENNUNGEN UND CODES, NIE TEXT.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from solvio.conversation.store import ConversationStoreError
from solvio.secret_vault.firewall import TRANSCRIPT_MARKER
from solvio.logging_setup import get_logger

log = get_logger("conversation")

#: Wie viele Chats gleichzeitig eine Zustellung verarbeiten. Jede Antwort ist
#: ein CLI-Prozess des Abos.
MAX_PARALLEL_CHATS = 2
#: Die Frist um die claimfreien Phasen (Store, Register, Auskunft, Vorbereitung).
PREP_TIMEOUT = 30.0
#: Beim Core-Stopp duerfen laufende Zustellungen noch kurz fertig werden. Danach
#: greift der vorhandene Launcher-Abbruch samt Prozessgruppen-Reap (kein Detach).
SHUTDOWN_GRACE = 5.0
#: Bisheriger Standard fuer fehlendes/spezialisiertes Profil und fuer Anhaenge.
#: Allgemeine Textauftraege verwenden ausdruecklich `task`; jede Auswahl steht
#: in `dispatch.scope`, damit ein spaeterer Wechsel keinen Replay bricht.
DEFAULT_CHAT_TASK_SCOPE = "research"
#: Wie lange eine Aktivitaet des Textwegs offen sein darf.
ACTIVITY_LIFETIME_SECONDS = 900
ACTIVITY_PURPOSE = "text_chat"

ACTION_QUESTION = "frage"
ACTION_CLARIFY = "rueckfrage"
ACTION_TASK = "auftrag"
ACTION_FOLLOWUP = "aenderung"
ACTION_AMBIGUOUS = "mehrdeutig"
ACTION_TOO_LONG = "objective_too_long"
ACTION_CHAIN_LOST = "kette_verloren"
#: Ein Anhang ausserhalb eines neuen Auftrags: benannt, NICHT als Rueckfrage gebucht
#: (Review Runde 19, F19-1: als `clarification` haette die naechste Nachricht am
#: verweigerten Satz gehangen und einen Auftrag ohne den Anhang gebunden).
ACTION_ATTACHMENT_UNUSED = "anhang_unbenutzt"
#: Die Routen, aus denen ein Auftrag wird.
# Current short questions need the same authenticated native research path.
# A classification as kurzrecherche must not become a toolless text answer.
_TASK_ROUTES = ("auftrag_recherche", "auftrag_bau", "kurzrecherche")

ERROR_SOURCE_REVOKED = "source_revoked"
ERROR_COST_RECOVERY = "cost_recovery_required"
ERROR_RESTARTED = "core_restarted_unresolved"
ERROR_TIMEOUT = "processing_timeout"
ERROR_QUOTA = "quota"
ERROR_PROVIDER = "provider_unavailable"
ERROR_OUTPUT = "provider_output_invalid"
ERROR_ASSESSMENT = "assessment_unavailable"
ERROR_FOLLOWUP = "followup_not_available"
#: Ein Anhang wirkt nur in einem NEUEN Auftrag (`_start_task` bindet ihn); eine
#: Folgeanweisung, ein expliziter Auftragsbezug und eine Frage tragen ihn nicht —
#: bis Review Runde 18 (B18-1) meldete SOLVIO dort Erfolg, waehrend der Anhang
#: still liegen blieb.
ATTACHMENT_NEEDS_TASK_TEXT = ("Den Anhang kann ich nur mit einem neuen Auftrag verwenden — an einer "
                              "Folgeanweisung oder Frage bleibt er unbenutzt, und das sage ich lieber, "
                              "als so zu tun. Schreib mir den Auftrag mit dem Anhang neu, oder schick "
                              "die Nachricht ohne Anhang.")
#: Eine verlorene Kette mit Anhang: den Anhang bitte erneut mitschicken (B20-2, Rest).
CHAIN_LOST_ATTACHMENT_SUFFIX = " Den Anhang bitte erneut mitschicken."
#: Fuer einen unerwarteten Fehler in einer claimfreien Phase — ehrlich benannt
#: statt als Zeitueberschreitung verkleidet.
ERROR_FAILED = "processing_failed"

TASK_ACCEPTED_TEXT = "Ich habe das aufgenommen und melde mich, wenn ich durch bin."
FOLLOWUP_ACCEPTED_TEXT = "Ich arbeite im selben Auftrag weiter."
CREDENTIAL_TEXT = ("Zugangsdaten nehme ich im Chat nicht an — sie wurden nicht gespeichert. "
                   "Fuer Passwoerter und Schluessel gibt es den Tresor.")
CHAIN_LOST_TEXT = ("Ich habe deinen urspruenglichen Satz zu dieser Rueckfrage nicht mehr. "
                   "Bitte formuliere den Auftrag noch einmal ganz — dann nehme ich ihn auf.")
TOO_LONG_TEXT = ("Das ist mir fuer einen Auftrag zu lang. Bitte fasse es in hoechstens "
                 "2000 Zeichen zusammen — dann nehme ich es auf.")
BUILD_NEEDS_PROJECT_TEXT = ("Bauauftraege nehme ich im Chat noch nicht an, weil hier kein Projekt "
                            "gewaehlt werden kann: bitte unter Auftraege > Programmieren mit "
                            "Projektauswahl starten. Oder sag mir, was ich stattdessen tun soll.")
RUNNING_TEXT = ("Dieser Auftrag laeuft noch. Soll ich ihn abbrechen und neu starten, "
                "oder warten, bis er fertig ist?")
NOT_CONTINUABLE_TEXT = ("Dieser Auftragsstand laesst sich so nicht fortsetzen. Die "
                        "Auftragskarte im Verlauf zeigt, was er gerade braucht.")


@dataclass(frozen=True)
class Runtime:
    """Was der Prozessor zur Verarbeitung braucht — spaet aufgeloest, nie eingesammelt.

    `orchestrator` ist die Agentenlaufzeit (Buecher, Kosten, Auftragsstart,
    Faehigkeitsrouter), `store` der Gespraechsspeicher, `router` der kognitive
    Router, `control_plane` die Kontrollebene (Core-ID, Geraetestand),
    `browser_service` der Browsersitzungsdienst, `transport` der Abo-Transport
    (`payload -> {ok, text, reason, tokens, cost_*}`), `personal_memory` der
    lesende Gedaechtnisdienst oder None.
    """
    orchestrator: Any
    store: Any
    router: Any
    control_plane: Any = None
    browser_service: Any = None
    transport: Any = None
    personal_memory: Any = None
    owner_principal: str = ""


def content_digest(conversation_id: str, message_id: str, sequence: int, text: str) -> str:
    """Der replay-stabile Aktivitaets-Digest (§3.2): Kennungen, Sequenz, Text.

    Nie das Kontextfenster: das aenderte sich, sobald eine Folgenachricht
    angenommen ist, und `admit` wuerfe beim Replay `activity_replay_binding_mismatch`.
    """
    from solvio.security.mobile_approval import protocol as P
    body = {"conversation_id": conversation_id, "message_id": message_id,
            "sequence": int(sequence),
            "text_sha256": hashlib.sha256(str(text).encode("utf-8")).hexdigest()}
    return hashlib.sha256(P.canonical_bytes(body)).hexdigest()


#: Wie viele Rueckfrage-Glieder ein Auftragstext hoechstens zusammenfasst. Drei
#: Rueckfragen hintereinander sind schon ein Gespraech, das neu beginnen sollte.
MAX_CLARIFICATION_CHAIN = 3


class ChainLost(Exception):
    """Ein Glied der Rueckfragekette ist nicht mehr lesbar — kein Speicherfehler,
    sondern ein Zustand des Gespraechs (Sprach-Turn ohne Nutzerzeile, Zustellung
    ohne Nachricht). Die Antwort darauf ist eine ehrliche Rueckfrage, die die Kette
    zuruecksetzt (Review Runde 4, R4-F-1)."""


def _dispatch_json(dispatch: dict[str, Any]) -> str:
    return json.dumps(dispatch, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _load_dispatch(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("action_class") else None


def _error_code(reason: str) -> str:
    """Die Antwort des Transports in das geschlossene Vokabular der Zustellung."""
    from solvio.cognition import taxonomy as TX
    reason = str(reason or "")
    if reason == ERROR_COST_RECOVERY:
        return ERROR_COST_RECOVERY
    if TX.quota_denied(reason) or reason == ERROR_QUOTA:
        return ERROR_QUOTA
    if reason in ("provider_output_invalid", "provider_output_truncated"):
        return ERROR_OUTPUT
    return ERROR_PROVIDER


class _SourceGate:
    """Der Liveness-Check im Kostenrahmen — mit Gedaechtnis, WAS er befand."""

    __slots__ = ("_processor", "_row", "_runtime", "revoked")

    def __init__(self, processor: "ConversationProcessor", row: dict[str, Any],
                 runtime: Runtime) -> None:
        self._processor, self._row, self._runtime = processor, row, runtime
        self.revoked = False

    async def __call__(self) -> bool:
        if not await self._processor.current(self._row, self._runtime):
            self.revoked = True
            raise ValueError(ERROR_SOURCE_REVOKED)
        return True


class ConversationProcessor:
    """Ein Worker je Chat, der Store als Warteschlange, eine Prozessgeneration."""

    def __init__(self, resolve: Callable[[], Runtime | None], *,
                 max_parallel: int = MAX_PARALLEL_CHATS,
                 prep_timeout: float = PREP_TIMEOUT,
                 shutdown_grace: float = SHUTDOWN_GRACE) -> None:
        self._resolve = resolve
        self.worker_generation = "pw-" + secrets.token_hex(8)
        self._workers: dict[str, asyncio.Task] = {}
        self._dirty: dict[str, asyncio.Event] = {}
        self._max_parallel = max(1, int(max_parallel))
        self._sem: asyncio.Semaphore | None = None
        self._prep_timeout = float(prep_timeout)
        self._shutdown_grace = max(0.0, float(shutdown_grace))
        self._stopping = False
        self._recovery: asyncio.Task | None = None
        self._shutdown: asyncio.Task | None = None
        #: Zaehler — nur fuer Betriebssicht und Tests, nie fuer Entscheidungen.
        self.stats = {"processed": 0, "completed": 0, "blocked": 0, "lost": 0,
                      "classified": 0, "answered": 0}

    # ----------------------------------------------------------------- Lebenszyklus
    def runtime(self) -> Runtime | None:
        try:
            value = self._resolve()
        except Exception as exc:  # noqa: BLE001 - ein Provider darf nie den Loop kippen
            log.warning("conversation.runtime_unresolved", kind=type(exc).__name__)
            return None
        if value is None or value.orchestrator is None or value.store is None or value.router is None:
            return None
        return value

    def start(self) -> None:
        """Beim Start des Endpunkts: die Wiederaufnahme anstossen, sobald die
        Laufzeit da ist. Der Gateway steht Sekunden vor dem Orchestrator."""
        if self._recovery is None or self._recovery.done():
            self._recovery = asyncio.get_running_loop().create_task(self._recover_when_ready())

    def stop(self) -> None:
        """Annahmestopp ohne Warten; der Lebenszyklus wartet danach `shutdown()` ab."""
        self._stopping = True
        if self._recovery is not None and not self._recovery.done():
            self._recovery.cancel()

    async def shutdown(self) -> None:
        """Vor dem Schliessen des Stores auslaufen lassen, dann abbrechen und reapen.

        Die Auslaufzeit ist begrenzt; anschliessend besitzt der vorhandene Starter
        seinen Abbruch bis zum Reap (TERM-Frist dort hoechstens fuenf Sekunden).
        Auch ein erneut abgebrochener Shutdown darf diese Kinder nicht abhaengen.
        Persistierte Zustellungen/Claims bleiben beim bestehenden Neustartvertrag.
        """
        self.stop()
        if self._shutdown is None or self._shutdown.done():
            self._shutdown = asyncio.create_task(self._stop_workers())
        cleanup = self._shutdown
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _stop_workers(self) -> None:
        owned = set(self._workers.values())
        if self._recovery is not None:
            owned.add(self._recovery)
        if not owned:
            return
        _, pending = await asyncio.wait(owned, timeout=self._shutdown_grace)
        for worker in pending:
            worker.cancel()
        # Cancellation laeuft durch Launcher und Kostentor, bevor der Store zugeht.
        await asyncio.gather(*owned, return_exceptions=True)

    async def _recover_when_ready(self, *, poll: float = 0.5, limit: float = 600.0) -> None:
        deadline = time.monotonic() + limit
        while not self._stopping and time.monotonic() < deadline:
            if self.runtime() is not None:
                try:
                    await self.recover()
                except Exception as exc:  # noqa: BLE001 - die Wiederaufnahme stirbt nie ungeloggt (Runde 10, K10-4)
                    log.error("conversation.recovery_failed", kind=type(exc).__name__, code=str(exc)[:80])
                return
            await asyncio.sleep(poll)
        log.warning("conversation.recovery_skipped", reason="runtime_unavailable")

    async def recover(self) -> int:
        """Jeden Chat mit offenen Zustellungen wecken. Nie erneut klassifizieren —
        was mit einer Waise geschieht, steht in ihrer Zeile (§3.1)."""
        runtime = self.runtime()
        if runtime is None:
            return 0
        try:
            chats = await asyncio.to_thread(runtime.store.conversations_with_open_deliveries)
        except ConversationStoreError as exc:
            log.error("conversation.recover_unreadable", code=str(exc)[:80])
            return 0
        for conversation_id in chats:
            self.wake(conversation_id)
        # A completed answer can survive a crash immediately before its learning
        # offer. The marker and answer share one store transaction.
        after_id = ""
        while not self._stopping:
            rows = await asyncio.to_thread(runtime.store.pending_learning, after_id=after_id)
            if not rows:
                break
            for row in rows:
                await self._offer_learning(row, runtime)
            after_id = rows[-1]["delivery_id"]
        log.info("conversation.recovered", chats=len(chats), generation=self.worker_generation)
        return len(chats)

    async def wait_idle(self, timeout: float = 30.0) -> bool:
        """Fuer Tests und den Abbau: warten, bis kein Worker mehr laeuft."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            live = [t for t in self._workers.values() if not t.done()]
            if not live:
                return True
            await asyncio.wait(live, timeout=max(0.0, deadline - time.monotonic()))
        return not any(not t.done() for t in self._workers.values())

    # ----------------------------------------------------------------- Wecken und Abarbeiten
    def wake(self, conversation_id: str) -> None:
        """Synchron auf dem Loop: Ereignis setzen, Worker anlegen, wenn keiner laeuft.

        Weil `wake` und der Austrag am Ende von `_drain` beide synchron auf dem
        Loop laufen, geht kein Wecken verloren; doppeltes Wecken ist wirkungslos.
        """
        conversation_id = str(conversation_id or "")
        if not conversation_id or self._stopping:
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        event = self._dirty.get(conversation_id)
        if event is None:
            event = self._dirty[conversation_id] = asyncio.Event()
        event.set()
        worker = self._workers.get(conversation_id)
        if worker is None or worker.done():
            self._workers[conversation_id] = asyncio.get_running_loop().create_task(
                self._drain(conversation_id), name=f"conversation-{conversation_id}")

    async def _drain(self, conversation_id: str) -> None:
        event = self._dirty[conversation_id]
        try:
            while not self._stopping:
                event.clear()
                async with self._sem:
                    if self._stopping:
                        break                   # ein wartender Chat beginnt nichts mehr
                    runtime = self.runtime()
                    if runtime is None:
                        log.warning("conversation.worker_without_runtime",
                                    conversation_id=conversation_id)
                        break
                    try:
                        row = await asyncio.to_thread(runtime.store.claim_next_delivery,
                                                      conversation_id, self.worker_generation)
                    except ConversationStoreError as exc:
                        log.error("conversation.claim_failed", conversation_id=conversation_id,
                                  code=str(exc)[:80])
                        break
                    if self._stopping:
                        # Der Store-Claim kann den Annahmestopp ueberholt haben.
                        # Seine persistierte Zeile gehoert dem Neustartvertrag;
                        # hier beginnt weder ein Modellaufruf noch eine Wirkung.
                        break
                    if row is not None:
                        await self._process(row, runtime)
                        continue
                # Kein Treffer, Semaphore frei: OHNE await pruefen, ob inzwischen
                # geweckt wurde — sonst geht genau dieses Wecken verloren.
                if event.is_set():
                    continue
                break
        finally:
            if self._workers.get(conversation_id) is asyncio.current_task():
                self._workers.pop(conversation_id, None)
                if self._stopping or not event.is_set():
                    # Beim Abbau wird NICHT nachgeholt: ein neuer Worker betraete
                    # die Schleife nie (`while not self._stopping`), liesse das
                    # Ereignis gesetzt und legte sich in genau diesem `finally`
                    # endlos neu an — gemessen 37230 Neuanlagen in 0,5 s, ein
                    # Busy-Loop bis zum Prozessende. Die Zustellung bleibt im
                    # Store `accepted`/`running`; `recover()` des naechsten
                    # Starts weckt sie.
                    self._dirty.pop(conversation_id, None)
                else:
                    # Ein Wecken traf ein, waehrend wir beendet haben: nachholen.
                    self._workers[conversation_id] = asyncio.get_running_loop().create_task(
                        self._drain(conversation_id), name=f"conversation-{conversation_id}")

    # ----------------------------------------------------------------- Liveness
    async def current(self, delivery: dict[str, Any], runtime: Runtime) -> bool:
        """Core-ID, Quellen-Generation und Chat-Eigentum — ohne Cookie, ohne Nonce (§3.2)."""
        cp = runtime.control_plane
        if cp is None or str(getattr(cp, "core_instance_id", "") or "") != delivery["core_id"]:
            return False
        principal = delivery["principal"]
        if delivery["source_kind"] == "dashboard":
            from solvio.browser_voice_session import browser_generation
            reference = str(delivery["source_ref"] or "")
            if not reference.startswith("browser:"):
                return False
            actual = await browser_generation(runtime.browser_service, reference[len("browser:"):],
                                              principal)
            if actual is None or actual != delivery["source_generation"]:
                return False
        else:
            from solvio.conversation.message_proof import source_fingerprint
            from solvio.voice_task_session import device_generation
            if not delivery["device_id"]:
                return False
            actual = await device_generation(cp, delivery["device_id"])
            if actual is None or source_fingerprint(actual) != delivery["source_generation"]:
                return False
            if actual[0] != principal:
                return False
        try:
            return bool(await asyncio.to_thread(runtime.store.conversation_owned,
                                                delivery["conversation_id"], principal))
        except ConversationStoreError:
            return False

    async def _learning_observation(self, row: dict[str, Any], runtime: Runtime):
        """Reconstruct ONLY this persisted user input, never answer/context/attachments."""
        from solvio.agent_runtime.cost_subjects import _verified_source
        from solvio.memory.adaptive.policy import OwnerTurn
        if not runtime.owner_principal or row['principal'] != runtime.owner_principal:
            return None

        async def original():
            saved = await asyncio.to_thread(runtime.store.delivery, row['conversation_id'], row['delivery_id'])
            if saved is None or not await self.current(saved, runtime):
                return None
            decision = _load_dispatch(saved['dispatch']) or {}
            if (saved['status'] != 'completed' or saved['task_id'] or saved['run_id'] or saved['attachments']
                    or not saved['assistant_message_id'] or decision.get('action_class') != ACTION_QUESTION
                    or decision.get('learning') not in {'pending', 'offered'}
                    or any(saved[k] != row[k] for k in ('principal', 'source_kind', 'source_ref',
                        'core_id', 'source_generation', 'device_id', 'message_id', 'message_sequence'))):
                return None
            message = await asyncio.to_thread(runtime.store.message, saved['conversation_id'], saved['message_id'])
            if message is None or message['role'] != 'user' or not await self.current(saved, runtime):
                return None
            return message['text']

        text = await original()
        if text is None or row['source_kind'] not in {'app', 'dashboard'}:
            return None
        turn = OwnerTurn(text, 'chat_iphone' if row['source_kind'] == 'app' else 'chat_dashboard',
            conversation_id=row['conversation_id'], message_id=row['message_id'],
            session_id=row['source_ref'], turn_id=row['delivery_id'])
        source = _verified_source(principal=row['principal'], source_kind=row['source_kind'],
            source_ref=row['source_ref'], conversation_id=row['conversation_id'], message_id=row['message_id'])

        async def valid():
            return await original() == text

        return turn, source, valid

    async def _offer_learning(self, row: dict[str, Any], runtime: Runtime) -> None:
        """Durable admission hands recovery to AdaptiveObservations, including holds."""
        from solvio.memory.adaptive.observations import observation_digest
        observations = getattr(runtime.orchestrator, 'memory_observations', None)
        if observations is None or not observations.adaptive.enabled:
            return
        try:
            observation = await self._learning_observation(row, runtime)
            if observation is None:
                return
            turn, source, valid = observation
            digest = observation_digest(turn)
            admitted = observations.activities.observation(source, digest)
            if admitted is None:
                try:
                    observations.offer(turn, source, validate=valid)
                except Exception as exc:  # durable admission may already have succeeded
                    log.warning('conversation.learning_offer_failed', kind=type(exc).__name__)
                admitted = observations.activities.observation(source, digest)
            if admitted is not None:
                if (admitted['state'] == 'pending' and not admitted['held_reason']
                        and admitted['activity_id'] not in observations._queued):
                    observations.activities.hold(admitted['activity_id'], 'observation_queue_unavailable')
                await asyncio.to_thread(runtime.store.learning_offered, row['delivery_id'])
        except Exception as exc:  # learning must not undo a completed answer
            log.warning('conversation.learning_unavailable', kind=type(exc).__name__)

    async def resume_learning(self, activity_id: str, principal: str) -> bool:
        """Existing owner endpoint, same persisted source; no replacement proof."""
        runtime = self.runtime()
        observations = getattr(runtime.orchestrator, 'memory_observations', None) if runtime else None
        if observations is None:
            raise ValueError('observation_resume_unavailable')
        held = observations.activities.held_chat(activity_id, principal)
        if held is None:
            raise ValueError('observation_resume_unavailable')
        row = await asyncio.to_thread(runtime.store.learning_delivery, principal,
                                     held['conversation_id'], held['message_id'])
        observation = await self._learning_observation(row, runtime) if row else None
        if observation is None:
            raise ValueError('observation_source_inactive')
        turn, source, valid = observation
        return observations.resume_chat(activity_id, principal, turn, source, validate=valid)

    # ----------------------------------------------------------------- Eine Zustellung
    async def _process(self, row: dict[str, Any], runtime: Runtime) -> None:
        delivery_id = row["delivery_id"]
        conversation_id = row["conversation_id"]
        self.stats["processed"] += 1
        log.info("conversation.delivery_started", delivery_id=delivery_id,
                 conversation_id=conversation_id, resumed=bool(row["dispatch"]))
        try:
            await self._process_guarded(row, runtime)
        except ConversationStoreError as exc:
            code = str(exc)
            if "delivery_lost" in code:
                self.stats["lost"] += 1
                log.warning("conversation.delivery_lost", delivery_id=delivery_id)
            else:
                log.error("conversation.store_failed", delivery_id=delivery_id, code=code[:80])
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("conversation.processing_timeout", delivery_id=delivery_id)
            await self._block_from_failure(row, runtime, ERROR_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - eine Zustellung darf den Worker nie kippen
            log.error("conversation.processing_failed", delivery_id=delivery_id,
                      kind=type(exc).__name__)
            await self._block_from_failure(row, runtime, ERROR_FAILED)

    async def _block_from_failure(self, row: dict[str, Any], runtime: Runtime, error_code: str) -> None:
        """Blockieren aus einem Fehlerzweig von `_process`. Stirbt der Store genau
        dabei, bleibt die Zustellung `running` fuer die Wiederaufnahme — geloggt mit
        ihrer Kennung, nie als unbeobachtete Task-Ausnahme, die den Worker dieses
        Chats still beendet (Review Runde 11, C11-H2; Runde 12, C12-H2)."""
        try:
            await self._block(row, runtime, error_code,
                              activity_id=await self._persisted_activity_id(row, runtime))
        except ConversationStoreError as exc:
            log.error("conversation.store_failed", delivery_id=row["delivery_id"], stage="block",
                      code=str(exc)[:80])

    async def _persisted_activity_id(self, row: dict[str, Any], runtime: Runtime) -> str:
        """Die Aktivitaet dieser Zustellung, wie der Store sie JETZT kennt.

        `row` ist der Stand vom Claim-Zeitpunkt; `_admit` legt die Aktivitaet erst
        danach an und persistiert sie mit `record_activity`. Ein Catch-all, der nur
        `row` befragt, liesse sie bis zum Ablauf offen — und das Downgrade-Skript
        verweigerte solange (§3.4, §1.6: eine terminale Zustellung hinterlaesst keine
        offene Aktivitaet). Fail-soft: ohne Store-Antwort bleibt der Claim-Stand.
        """
        try:
            current = await asyncio.to_thread(runtime.store.delivery, row["conversation_id"], row["delivery_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("conversation.activity_lookup_failed", kind=type(exc).__name__)
            current = None
        return str((current or {}).get("activity_id") or row.get("activity_id") or "")

    async def _prep(self, coro):
        """Eine Frist um eine claimfreie Phase."""
        return await asyncio.wait_for(coro, timeout=self._prep_timeout)

    async def _process_guarded(self, row: dict[str, Any], runtime: Runtime) -> None:
        store = runtime.store
        delivery_id, conversation_id = row["delivery_id"], row["conversation_id"]
        # (a) Liveness zu Beginn jeder (Re-)Verarbeitung.
        try:
            live = await self._prep(self.current(row, runtime))
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT, activity_id=row["activity_id"])
            return
        if not live:
            await self._block(row, runtime, ERROR_SOURCE_REVOKED, activity_id=row["activity_id"])
            return
        message = await self._prep(asyncio.to_thread(store.message, conversation_id, row["message_id"]))
        if message is None or not str(message.get("text") or "").strip():
            await self._block(row, runtime, ERROR_FAILED, activity_id=row["activity_id"])
            return
        # DER VERARBEITUNGSTEXT IST DER PERSISTIERTE, REDIGIERTE TEXT — nie der Body.
        text = str(message["text"])
        dispatch = _load_dispatch(row["dispatch"])
        activity_id = str(row["activity_id"] or "")
        if text == TRANSCRIPT_MARKER and dispatch is None:
            # Der Tresor-Zaun hat Zugangsdaten ersetzt: den Platzhalter weder routen (ein
            # Modellaufruf auf nichts) noch „beantworten“ — eine feste Auskunft, kein
            # Auftrag, keine Aktivitaet noetig (Review Runde 4, B-H2).
            log.info("conversation.credential_placeholder", delivery_id=delivery_id)
            await self._complete(row, runtime, assistant_text=CREDENTIAL_TEXT, activity_id=activity_id)
            return
        if dispatch is None:
            if activity_id:
                verdict = await asyncio.to_thread(self._orphan_verdict, runtime, activity_id, 0)
                if verdict:
                    await self._block(row, runtime, verdict, activity_id=activity_id)
                    return
            target = _load_target(row["target"])
            if target is not None:
                dispatch = await self._explicit_followup_dispatch(row, text, target, runtime)
            else:
                dispatch = await self._route(row, text, runtime)
            if dispatch is None:
                return                       # bereits blockiert
            activity_id = dispatch.get("activity_id") or activity_id
        action = dispatch.get("action_class")
        if action in (ACTION_CLARIFY, ACTION_AMBIGUOUS, ACTION_TOO_LONG, ACTION_CHAIN_LOST, ACTION_ATTACHMENT_UNUSED):
            await self._complete(row, runtime, assistant_text=str(dispatch.get("assistant_text") or ""),
                                 activity_id=activity_id)
        elif action == ACTION_QUESTION:
            await self._answer(row, text, dispatch, runtime, activity_id)
        elif action == "mail":
            from solvio.conversation.mail import execute
            await execute(self, row, dispatch, runtime, activity_id)
        elif action == ACTION_TASK:
            await self._start_task(row, dispatch, runtime, activity_id)
        elif action == ACTION_FOLLOWUP:
            await self._followup(row, dispatch, runtime, activity_id)
        else:
            await self._block(row, runtime, ERROR_FAILED, activity_id=activity_id)

    # ----------------------------------------------------------------- Kosten und Routing
    def _activities(self, runtime: Runtime):
        from solvio.agent_runtime import cost_subjects as A
        return A.ActivityLedger(runtime.orchestrator.ledger)

    def _invocations(self, runtime: Runtime, activity_id: str) -> list[dict[str, Any]]:
        with runtime.orchestrator.ledger._open() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM agent_provider_invocations WHERE activity_id=? ORDER BY ordinal",
                (activity_id,))]

    def _orphan_verdict(self, runtime: Runtime, activity_id: str, after_ordinal: int) -> str:
        """Was der Claim-Befund einer Waise erzwingt (§3.1-Tabelle) — oder ``."""
        rows = [r for r in self._invocations(runtime, activity_id) if int(r["ordinal"]) > after_ordinal]
        if any(r["state"] in ("claimed", "unknown") for r in rows):
            return ERROR_COST_RECOVERY
        if any(r["state"] == "finished" for r in rows):
            return ERROR_RESTARTED
        return ""

    def _unusable_code(self, runtime: Runtime, activity_id: str, exc: ValueError) -> str:
        """Ergebniswahrheit fuer eine unbrauchbare Aktivitaet: ist sie nur ABGELAUFEN und
        hat sie nie einen Claim gesehen, war kein Geld im Spiel — das ist ein
        `core_restarted_unresolved` (Neustart nach der Lebensdauer), kein ungewisser
        Kostenausgang. Jeder andere Grund bleibt `cost_recovery_required` (§3.1-Tabelle)."""
        if str(exc) == "activity_expired" and activity_id:
            try:
                if not self._invocations(runtime, activity_id):
                    return ERROR_RESTARTED
            except Exception:  # noqa: BLE001 - im Zweifel der vorsichtigere Code
                pass
        return ERROR_COST_RECOVERY

    def _origin(self, row: dict[str, Any]) -> str:
        return "trusted_dashboard" if row["source_kind"] == "dashboard" else "trusted_interactive_app"

    def _admit(self, runtime: Runtime, row: dict[str, Any], text: str):
        from solvio.agent_runtime import cost_subjects as A
        source = A._verified_source(principal=row["principal"], source_kind=row["source_kind"],
                                    source_ref=row["source_ref"],
                                    conversation_id=row["conversation_id"],
                                    message_id=row["message_id"])
        digest = content_digest(row["conversation_id"], row["message_id"],
                                int(row["message_sequence"]), text)
        binding = self._activities(runtime).admit(
            source, content_digest=digest, purpose=ACTIVITY_PURPOSE,
            operation_key=row["delivery_id"], lifetime_seconds=ACTIVITY_LIFETIME_SECONDS)
        runtime.store.record_activity(row["delivery_id"], self.worker_generation, binding.activity_id)
        return binding

    def _scope(self, runtime: Runtime, binding, row: dict[str, Any]):
        """Der Kostenrahmen eines Modellaufrufs (`InteractionCostScope`, Zweck `text_chat`)
        mit den Quote-/Settlement-Adaptern des Orchestrators — kein neuer Kostenadapter."""
        from solvio.agent_runtime import cost_dispatch as D
        del row
        orch = runtime.orchestrator
        return D.interaction_cost_scope(
            orch.ledger, activity_id=binding.activity_id, content_digest=binding.content_digest,
            quote_adapter=getattr(orch, "cost_quote_adapter", None),
            settlement_adapter=getattr(orch, "cost_settlement_adapter", None))

    def _source_check(self, row: dict[str, Any], runtime: Runtime) -> "_SourceGate":
        """`scope.source_check` (§3.2 b) — und die Ergebniswahrheit dazu.

        `cost_dispatch.dispatch` faengt JEDE Ausnahme des Checks und sagt
        `cost_recovery_required`; das Vokabular des Kostentors kennt keine
        Quelle. Ein Browser-Logout waehrend des Routings (bis 120 s je CLI-Aufruf)
        oder vor dem Antwort-Claim hiesse dem Nutzer sonst „Kostenpruefung
        erforderlich" statt „Anmeldung abgelaufen" (§3.2: Fehlschlag →
        `source_revoked`). Deshalb merkt sich das Tor selbst, ob ES die Absage
        war: `revoked` wird nur gesetzt, wenn `current()` tatsaechlich False
        ergab — nie bei einer Ausnahme darin (die bleibt ungewiss).
        """
        return _SourceGate(self, row, runtime)

    def _transport_adapter(self, runtime: Runtime, reasons: list[str]):
        """Der Abo-Transport in der Form, die der Einschaetzer erwartet.

        `reasons` sammelt die Absagegruende der Aufrufe: der Einschaetzer kennt
        nur das Broker-Vokabular (`rate_capped`, `token_capped`), der Abo-Weg sagt
        `quota` oder `cost_recovery_required` — und genau diese beiden entscheiden
        hier ueber `blocked` statt einer Antwort ohne Auftrag.
        """
        transport = runtime.transport

        async def adapter(payload: dict, token: str = "", port: int = 0) -> dict:
            del token, port                  # der Abo-Weg kennt weder Broker-Token noch Port
            result = await transport({"input": payload["input"]})
            if not result.get("ok"):
                reasons.append(str(result.get("reason") or ""))
            return result
        return adapter

    async def _route(self, row: dict[str, Any], text: str, runtime: Runtime) -> dict[str, Any] | None:
        """Routing (§3.3): admit → classify_text im Kostenrahmen → Entscheid persistieren."""
        from solvio.agent_runtime import cost_dispatch as D
        from solvio.cognition.router import AssessmentFailed
        delivery_id, conversation_id = row["delivery_id"], row["conversation_id"]
        started = time.monotonic()
        try:
            binding = await self._prep(asyncio.to_thread(self._admit, runtime, row, text))
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT)
            return None
        except ValueError as exc:
            log.warning("conversation.admit_refused", delivery_id=delivery_id, reason=str(exc)[:80])
            await self._block(row, runtime, ERROR_COST_RECOVERY)
            return None
        activity_id = binding.activity_id
        # Eine Waise mit Aktivitaet, aber ohne Entscheid, kam hier nur an, wenn
        # keine Invocation existiert (§3.1) — sonst waere sie oben blockiert.
        assessment = event = calls = view = None
        failure = ""
        reasons: list[str] = []
        self.stats["classified"] += 1
        gate = self._source_check(row, runtime)
        related = await self._related_context(row, text, runtime)
        try:
            with self._scope(runtime, binding, row) as scope:
                scope.source_check = gate
                try:
                    assessment, event, calls, view = await runtime.router.classify_text(
                        text, conversation_id, delivery_id,
                        transport=self._transport_adapter(runtime, reasons),
                        message_sequence=int(row["message_sequence"]), timeout=None,
                        related_context=related)
                except AssessmentFailed as exc:
                    event, calls = exc.event, []
                    failure = exc.kind
                    if ERROR_COST_RECOVERY in reasons or D.recovery_pending():
                        failure = ERROR_COST_RECOVERY
                    elif any(_error_code(reason) == ERROR_QUOTA for reason in reasons):
                        failure = "assessment_quota"
        except ValueError as exc:
            # Die Aktivitaet traegt nicht mehr (abgelaufen, gehalten, widerrufen).
            log.warning("conversation.activity_unusable", delivery_id=delivery_id,
                        reason=str(exc)[:80])
            await self._block(row, runtime, self._unusable_code(runtime, activity_id, exc),
                              activity_id=activity_id)
            return None
        from solvio.cognition.types import EscalationEvent
        if failure == ERROR_COST_RECOVERY:
            # Ergebniswahrheit: kam die Absage aus dem Quellen-Check, ist die Quelle
            # weg — nicht das Geld (§3.2, §5.4). Kein Claim fand statt.
            code = ERROR_SOURCE_REVOKED if gate.revoked else ERROR_COST_RECOVERY
            await self._block(row, runtime, code, activity_id=activity_id,
                              hold_reason=ERROR_SOURCE_REVOKED if gate.revoked else "")
            return None
        if failure == "assessment_quota":
            await self._block(row, runtime, ERROR_QUOTA, activity_id=activity_id,
                              hold_reason=failure)
            return None
        routing_ordinal = await asyncio.to_thread(self._max_ordinal, runtime, activity_id)
        if failure:
            # Routing gescheitert, kein Kontingentproblem: eine kurze Antwort ohne Task (§3.3)
            # — und mit wirksamem Anhang (eigene Zeile ODER offene Kette, Runde 21 F21-1)
            # keine blinde Antwort (Review Runde 19, F19-2). Die Kette wird VOR dem
            # eigenen Entscheid gelesen, der sie abschliesst.
            turn, kind = await asyncio.to_thread(self._attachment_turn, runtime, row, None)
            unused = bool(turn)
            decision_id = runtime.router.record_text_decision(
                conversation_ref=conversation_id, turn_ref=delivery_id, origin=self._origin(row),
                user_text=text, route_final="", outcome="handed_back", failure_kind="",
                event=event or EscalationEvent.NONE, calls=calls or (), started=started)
            dispatch = {"action_class": ACTION_ATTACHMENT_UNUSED if unused else ACTION_QUESTION, "route": "", "scope": "",
                        "attachment_kind": kind, "attachment_turn": turn, "target_task_id": "",
                        "target_run_id": "", "assistant_text": ATTACHMENT_NEEDS_TASK_TEXT if unused else "",
                        "decision_id": decision_id, "objective": text, "routing_invocations": routing_ordinal,
                        "activity_id": activity_id}
            await asyncio.to_thread(runtime.store.record_dispatch, delivery_id,
                                    self.worker_generation, _dispatch_json(dispatch))
            return dispatch
        dispatch = await self._decide(row, text, assessment, view, runtime)
        from solvio.conversation import mail
        if mail.eligible(row, dispatch):
            # Reuse this delivery's existing subscription/cost scope; one choice,
            # durably accounted before any draft or approval. No native web tools.
            try:
                with self._scope(runtime, binding, row) as scope:
                    for _ in range(routing_ordinal):
                        scope.next_invocation()
                    scope.source_check = gate
                    history = await asyncio.to_thread(runtime.store.recent_context,
                        row['conversation_id'], max_chars=6000, before_sequence=int(row['message_sequence']))
                    selected = await runtime.transport(mail.payload(dispatch["objective"], history))
                if not await self.current(row, runtime):
                    await self._block(row, runtime, ERROR_SOURCE_REVOKED, activity_id=activity_id)
                    return None
                claim = await asyncio.to_thread(self._claim_row, runtime, selected.get("cost_reservation_id"))
                if (not selected.get("ok") or claim is None or claim["activity_id"] != activity_id
                        or claim["phase"] != ACTIVITY_PURPOSE or claim["state"] != "finished"):
                    await self._block(row, runtime, _error_code(selected.get("reason") or ERROR_COST_RECOVERY),
                                      activity_id=activity_id)
                    return None
                dispatch = mail.decide(str(selected.get("text") or ""), dispatch,
                                       source_kind=row["source_kind"])
                routing_ordinal = await asyncio.to_thread(self._max_ordinal, runtime, activity_id)
            except ValueError:
                await self._block(row, runtime, ERROR_OUTPUT, activity_id=activity_id)
                return None
        outcome = {ACTION_QUESTION: "handed_back", ACTION_CLARIFY: "clarification",
                   ACTION_AMBIGUOUS: "clarification", ACTION_TOO_LONG: "clarification",
                   ACTION_CHAIN_LOST: "handed_back",   # setzt die offene Rueckfrage zurueck
                   ACTION_ATTACHMENT_UNUSED: "handed_back",  # keine Kette an den verweigerten Satz (F19-1)
                   ACTION_TASK: "dispatched", "mail": "dispatched", ACTION_FOLLOWUP: "dispatched"}[dispatch["action_class"]]
        decision_id = runtime.router.record_text_decision(
            conversation_ref=conversation_id, turn_ref=delivery_id, origin=self._origin(row),
            user_text=text, route_final=assessment.route.value, outcome=outcome,
            produced_ref=dispatch["target_task_id"], continuity_ref=assessment.continuation_of,
            assessment=assessment, event=event, calls=calls, started=started)
        dispatch.update(decision_id=decision_id, routing_invocations=routing_ordinal,
                        activity_id=activity_id)
        await asyncio.to_thread(runtime.store.record_dispatch, delivery_id,
                                self.worker_generation, _dispatch_json(dispatch))
        log.info("conversation.dispatch_recorded", delivery_id=delivery_id,
                 action=dispatch["action_class"], route=dispatch["route"])
        return dispatch

    def _max_ordinal(self, runtime: Runtime, activity_id: str) -> int:
        rows = self._invocations(runtime, activity_id)
        return max((int(r["ordinal"]) for r in rows), default=0)

    async def _decide(self, row: dict[str, Any], text: str, assessment, view,
                      runtime: Runtime) -> dict[str, Any]:
        """Aus der Einschaetzung die Aktionsklasse (§3.3-Tabelle). Kein Modellaufruf.

        Ein Anhang, den die Entscheidung nicht in einen neuen Auftrag traegt, wird
        benannt statt verworfen (Review Runde 18, B18-1)."""
        decision = await self._decide_route(row, text, assessment, view, runtime)
        if decision["action_class"] in (ACTION_CLARIFY, ACTION_AMBIGUOUS):
            from solvio.conversation import related_context as RC
            candidates = list(getattr(view, "related_entries", []) or [])
            if candidates and getattr(assessment, "reference_intent", "neu") in ("fortsetzen", "unklar"):
                decision = dict(decision, related_candidates=RC.candidate_refs(candidates),
                    assistant_text=self._related_question(candidates))
        # Der wirksame Anhang: der dieser Zeile — oder der einer frueheren Zeile derselben
        # Rueckfragekette (Review Runde 20, B20-1: eine Rueckfrage an einen NEUEN Auftrag
        # mit Anhang bleibt eine Rueckfrage, und der Anhang reist mit in den Auftrag;
        # Runde 19 hatte jede solche Rueckfrage in eine Sackgasse verwandelt).
        turn, kind = await asyncio.to_thread(self._attachment_turn, runtime, row, view)
        if turn:
            decision = dict(decision, attachment_kind=kind, attachment_turn=turn)
            if decision["action_class"] == ACTION_TASK:
                # Datei-/Dokumentvertraege gehoeren weiterhin zum vorhandenen
                # Rechercheweg, auch wenn der Anhang aus einer Rueckfrage stammt.
                decision["scope"] = DEFAULT_CHAT_TASK_SCOPE
        if turn and decision["action_class"] in (ACTION_QUESTION, ACTION_FOLLOWUP):
            # Frage und Folgeanweisung tragen keinen Anhang (DEBT-0300): benennen, nie
            # blind antworten oder als Erfolg melden (B18-1, F19-1/F19-2).
            return dict(decision, action_class=ACTION_ATTACHMENT_UNUSED, assistant_text=ATTACHMENT_NEEDS_TASK_TEXT,
                        target_task_id="", target_run_id="")
        if turn and decision["action_class"] == ACTION_CHAIN_LOST:
            return dict(decision, assistant_text=CHAIN_LOST_TEXT + CHAIN_LOST_ATTACHMENT_SUFFIX)
        return decision

    def _pending_related_candidates(self, runtime: Runtime, row: dict[str, Any]):
        turn = self._open_clarification(runtime, row["conversation_id"])
        if not turn or turn == row["delivery_id"]:
            return None
        previous = runtime.store.delivery(row["conversation_id"], turn)
        dispatch = _load_dispatch((previous or {}).get("dispatch", "")) or {}
        candidates = dispatch.get("related_candidates")
        return candidates[:5] if isinstance(candidates, list) else None

    async def _related_context(self, row: dict[str, Any], text: str, runtime: Runtime,
                               *, for_answer: bool = False):
        from solvio.cognition.continuity import RelatedContext
        from solvio.conversation import related_context as RC
        from solvio.agent_runtime import personal_context
        if personal_context.excludes_personal_context(text):
            return RelatedContext()
        try:
            pending = None if for_answer else await self._prep(asyncio.to_thread(
                self._pending_related_candidates, runtime, row))
            related = await self._prep(asyncio.to_thread(
                RC.collect, runtime, row, text, pending_candidates=pending,
                allow_recent=not for_answer))
        except (ConversationStoreError, asyncio.TimeoutError, ValueError, OSError):
            log.info("conversation.related_context_unavailable", delivery_id=row["delivery_id"])
            related = RelatedContext(incomplete=True)
        if (not for_answer and runtime.personal_memory is not None and runtime.owner_principal
                and row['principal'] == runtime.owner_principal):
            from solvio.agent_runtime import personal_context
            memory = await self._prep(personal_context.for_call(
                runtime.personal_memory, query=text, max_chars=1200, purpose="chat_route"))
            if memory:
                related.context = (related.context + "\n" + memory).strip()
        return related

    @staticmethod
    def _related_question(candidates) -> str:
        choices = "; ".join(f"{number}. ‚{entry.summary[:60]}' (Chat ‚{entry.source_title[:40]}')"
                            for number, entry in enumerate(candidates[:5], 1))
        return f"Welchen bisherigen Auftrag meinst du: {choices}?"

    def _attachment_turn(self, runtime: Runtime, row: dict[str, Any], view) -> tuple[str, str]:
        """Die Zustellung, deren Anhang fuer diese Nachricht gilt, und seine Art: die
        eigene, sonst die juengste der offenen Rueckfragekette (auch eine abgewiesene
        Ueberlaenge, B20-2 — ihr Text wird ersetzt, ihr Anhang nicht). Leer, wenn keine
        traegt; ein unlesbares Glied beendet nur die Suche."""
        kind = _attachment_kind(row)
        if kind:
            return str(row["delivery_id"]), kind
        conversation_id = row["conversation_id"]
        pending = str(getattr(view, "pending_clarification_turn", "") or "")
        if view is None:
            # Ohne Continuity-Sicht (Routing gescheitert, expliziter Auftragsbezug) liest
            # das Entscheidungsbuch die offene Rueckfrage — dieselbe Regel wie in
            # cognition/continuity.py (Review Runde 21, F21-1).
            pending = self._open_clarification(runtime, conversation_id)
        if not pending or pending == row["delivery_id"]:
            return "", ""
        turn, seen = pending, set()
        while turn and turn not in seen and len(seen) < MAX_CLARIFICATION_CHAIN:
            seen.add(turn)
            delivery = runtime.store.delivery(conversation_id, turn)
            if delivery is None:
                turn = self._older_clarification(runtime, conversation_id, turn)   # ein Sprach-Turn
                continue
            kind = _attachment_kind(delivery)
            if kind:
                return turn, kind
            dispatch = json.loads(delivery["dispatch"]) if delivery.get("dispatch") else {}
            if dispatch.get("action_class") == ACTION_ATTACHMENT_UNUSED:
                return "", ""
            turn = str(dispatch.get("clarified_turn") or "")
        return "", ""

    async def _decide_route(self, row: dict[str, Any], text: str, assessment, view,
                            runtime: Runtime) -> dict[str, Any]:
        from solvio.capabilities.agent import MAX_OBJECTIVE, MIN_OBJECTIVE
        route = assessment.route.value
        kind = _attachment_kind(row)
        pending = str(getattr(view, "pending_clarification_turn", "") or "")
        base = {"action_class": ACTION_QUESTION, "route": route, "scope": "",
                "attachment_kind": kind, "target_task_id": "", "target_run_id": "",
                "assistant_text": "", "objective": text,
                # Der Turn, auf den SOLVIO zurueckgefragt hatte, als dieser Text kam —
                # damit eine spaetere Antwort die ganze Kette lesen kann (F-5).
                "clarified_turn": pending if pending != row["delivery_id"] else ""}
        if route == "klaerung":
            return dict(base, action_class=ACTION_CLARIFY, assistant_text=assessment.clarification)
        if route not in _TASK_ROUTES:
            return base
        try:
            objective = await asyncio.to_thread(self._user_words, runtime, row, text, view)
        except ChainLost as lost:
            log.warning("conversation.clarification_chain_lost", delivery_id=row["delivery_id"],
                        turn=str(lost)[:40])
            return dict(base, action_class=ACTION_CHAIN_LOST, assistant_text=CHAIN_LOST_TEXT)
        if len(objective) > MAX_OBJECTIVE:
            return dict(base, action_class=ACTION_TOO_LONG, assistant_text=TOO_LONG_TEXT)
        continuation = str(assessment.continuation_of or "")
        if continuation:
            # Mehrere fremde Kandidaten und ein Satz, der zu kurz ist, um zu
            # unterscheiden: dann waehlt nicht das Modell, dann fragt SOLVIO.
            # Die Mehrdeutigkeitsregel der Policy greift nur, wenn ALLE
            # Kandidaten aus dem Rueckfallweg stammen; bei zwei Thementreffern
            # entschied bisher allein die Einschaetzung (DEBT-0308, Review
            # Runde 22, Verifikation N-2b). Steht die Auswahlfrage schon offen,
            # IST diese kurze Antwort die Auswahl — dann nicht erneut fragen.
            # Den Fragetext setzt `_decide` aus denselben Kandidaten (`_related_question`).
            # Er wird hier trotzdem gebildet: ein Rueckfalltext ohne Auswahl waere eine
            # Frage ohne Ausweg (Review Runde 23, B4/K23B-7).
            related = list(getattr(view, "related_entries", []) or [])
            if (len(related) > 1 and len(objective) < MIN_OBJECTIVE
                    and not getattr(view, "related_selection_pending", False)
                    and continuation in {entry.work_id for entry in related}):
                return dict(base, action_class=ACTION_AMBIGUOUS,
                            assistant_text=self._related_question(related))
            target = await asyncio.to_thread(self._continuation_target, runtime, row, continuation,
                                             getattr(view, "related_entries", []))
            if target is None:
                names = self._task_names(runtime, row, [continuation])
                question = ("Meinst du den Auftrag " + names + "? Der laesst sich so nicht "
                            "fortsetzen. Soll ich stattdessen etwas Neues beginnen?")
                return dict(base, action_class=ACTION_AMBIGUOUS, assistant_text=question)
            decision = dict(base, action_class=ACTION_FOLLOWUP, objective=objective,
                            target_task_id=target[0], target_run_id=target[1])
            source = next((entry for entry in getattr(view, "related_entries", [])
                           if entry.work_id == continuation), None)
            current_links = await asyncio.to_thread(runtime.store.task_links, row["conversation_id"])
            if source is not None and continuation not in {link["task_id"] for link in current_links}:
                decision["source_chat"] = {"conversation_id": source.source_conversation_id,
                                           "title": source.source_title}
            return decision
        if len(objective) < MIN_OBJECTIVE:
            return base                      # nur ein NEUER Auftrag braucht zwoelf Zeichen
        active = sorted(view.active_ids()) if view is not None else []
        active = [task_id for task_id in active if task_id.startswith("at-")]
        if len(active) >= 2 and not view.pending_clarification_turn:
            names = self._task_names(runtime, row, active[:3])
            question = (f"Meinst du einen der laufenden Auftraege {names} — oder soll ich "
                        "etwas Neues beginnen?")
            return dict(base, action_class=ACTION_AMBIGUOUS, assistant_text=question)
        profile = str(getattr(assessment, "task_profile", "") or "")
        if profile == "projekt" or (route == "auftrag_bau" and profile != "allgemein"):
            return dict(base, action_class=ACTION_CLARIFY, assistant_text=BUILD_NEEDS_PROJECT_TEXT)
        if profile == "persoenlich":
            # Kurskorrektur S1 / ADR-0040: Postfach und Kalender nur fuer diesen
            # Auftrag, und dessen Sitzung ohne Webzugriff.
            return dict(base, action_class=ACTION_TASK, scope="task", objective=objective,
                        private_data=True)
        return dict(base, action_class=ACTION_TASK,
                    scope="task" if profile == "allgemein" else DEFAULT_CHAT_TASK_SCOPE,
                    objective=objective)

    def _user_words(self, runtime: Runtime, row: dict[str, Any], text: str, view) -> str:
        """Der Auftragstext (§3.5): die persistierten Worte des Nutzers — nie `assessment.objective`.

        Der Einschaetzer bildet sein Ziel aus den Worten des Menschen, und die
        Ueberlappungsregel (`policy.validate`, 0.7) laesst dabei fast ein
        Drittel eigene Worte durch. Fuer eine Antwort im Gespraech ist das in
        Ordnung; fuer den Parameter, der an einen Auftragsstart gebunden wird
        (`AuthorizedTaskStart.bind`, `request_digest`), ist es das nicht — was
        der Nutzer im Chat sieht und was hinausgeht, ist sein Satz, nicht eine
        Nachdichtung (Befund B-1 der unabhaengigen Pruefung vom 18.09.2026).

        Steht in diesem Chat SOLVIOs Rueckfrage offen, ist der Auftrag die
        ganze Kette: der Satz, auf den zurueckgefragt wurde, jede Antwort, die
        wieder eine Rueckfrage bekam, und diese Antwort — in Reihenfolge, alles
        Nutzertext aus dem Gespraechsspeicher, nie SOLVIOs Frage (sonst haette
        sich das Haus selbst beauftragt). Gelesen wird je Glied die Zustellung
        und IHRE Nachricht (`message_id`), nicht ein Fenster der juengsten
        Zeilen: eine Kette hat keine Fenstergrenze, und ein fehlendes Glied ist
        ein Fehler, kein kuerzerer Auftrag (Review Runde 2, F-5/F-6).

        Eine abgewiesene Ueberlaenge (`objective_too_long`) ist kein Satz, den
        die naechste Nachricht fortsetzt: die gekuerzte Fassung ERSETZT ihn
        (F-1 — vorher verkettete der Weg 2560 Zeichen vor die Kurzfassung und
        wies sie erneut ab).

        Ein Glied muss keine Zustellung sein: eine an diesen Chat gebundene
        SPRACHSITZUNG (§7) bucht ihre Rueckfrage mit dem Sprach-Turn als
        `turn_ref`; dann traegt der Verlauf die gesprochenen Worte unter
        `source_turn_id` (Review Runde 3, F3-1 — eine Fassung dazwischen warf
        hier eine Speicher-Ausnahme, und das Auffangnetz hielt sie fuer einen
        kaputten Speicher: die Zustellung blieb ewig `running`). Ein wirklich
        fehlendes Glied — oder ein Glied, das nur den Tresor-Platzhalter traegt
        (Review Runde 5, R5-H-1) — ist `ChainLost`: SOLVIO fragt ehrlich neu
        (`kette_verloren`, Outcome `handed_back`), nie ein kuerzerer oder
        sinnloser Auftrag, nie ein Dauerfehler (Review Runde 4, R4-F-1).
        """
        from solvio.cognition import models as M
        conversation_id = row["conversation_id"]
        pending = str(getattr(view, "pending_clarification_turn", "") or "")
        if not pending or pending == row["delivery_id"]:
            return text
        sequence = int(row["message_sequence"])
        earlier: list[str] = []
        seen: set[str] = set()
        turn = pending
        while turn and turn not in seen and len(earlier) < MAX_CLARIFICATION_CHAIN:
            seen.add(turn)
            delivery = runtime.store.delivery(conversation_id, turn)
            if delivery is None:
                # Sprach-Turn: die Nutzerzeilen dieses Turns, direkt und ohne Fenster
                # (R4-F-1). Fehlen sie (z. B. Barge-in-Fehlablage), ist die Kette
                # verloren — dann fragt SOLVIO ehrlich neu, statt einen halben
                # Auftrag zu binden oder jede weitere Nachricht scheitern zu lassen.
                spoken = runtime.store.turn_user_text(conversation_id, turn, upto_sequence=sequence)
                if not spoken or spoken == TRANSCRIPT_MARKER:
                    raise ChainLost(turn)
                earlier.append(spoken)
                # Ein Sprach-Turn traegt keinen Dispatch; ob davor noch eine Text-
                # Rueckfrage offen war, weiss das Entscheidungsbuch (R4-F-2): der
                # naechstaeltere Entscheid dieser Konversation, wenn er selbst eine
                # Rueckfrage war, ist das naechste Glied.
                turn = self._older_clarification(runtime, conversation_id, turn)
                continue
            dispatch = json.loads(delivery["dispatch"]) if delivery.get("dispatch") else {}
            if dispatch.get("action_class") in (ACTION_TOO_LONG, ACTION_ATTACHMENT_UNUSED):
                break
            message = runtime.store.message(conversation_id, str(delivery.get("message_id") or ""))
            if message is None or message.get("role") != "user":
                raise ChainLost(turn)
            earlier.append(str(message.get("text") or "").strip())
            turn = str(dispatch.get("clarified_turn") or "")
        earlier.reverse()
        return " ".join(part for part in [*earlier, text] if part)

    def _open_clarification(self, runtime: Runtime, conversation_id: str) -> str:
        """Die juengste offene Rueckfrage dieser Konversation aus dem Entscheidungsbuch:
        der erste Entscheid mit Outcome `clarification`, bevor ein `dispatched`/
        `handed_back`/`refused`/`failed` sie abschliesst — die Regel von
        `cognition/continuity.py`; leer bei unlesbarem Buch."""
        from solvio.cognition import models as M
        ledger = getattr(runtime.router, "ledger", None)
        if ledger is None:
            return ""
        try:
            decisions = ledger.recent(conversation_id, limit=M.REGISTER_MAX * 2)
        except Exception as exc:  # noqa: BLE001 - ein unlesbares Buch beendet die Suche, nicht die Zustellung
            log.info("conversation.register_unreadable", kind=type(exc).__name__)
            return ""
        for decision in decisions:
            if decision.get("outcome") == "clarification":
                return str(decision.get("turn_ref") or "")
            if decision.get("outcome") in ("dispatched", "handed_back", "refused", "failed"):
                break
        return ""

    def _older_clarification(self, runtime: Runtime, conversation_id: str, turn: str) -> str:
        """Der Entscheid unmittelbar vor `turn` in dieser Konversation — sein `turn_ref`,
        wenn er eine Rueckfrage war, sonst ''. Nur das eigene Gespraech (`recent` ist
        konversationsgebunden), nur Rueckfragen, nie Fremdes."""
        from solvio.cognition import models as M
        ledger = getattr(runtime.router, "ledger", None)
        if ledger is None:
            return ""
        try:
            decisions = ledger.recent(conversation_id, limit=M.REGISTER_MAX * 2)
        except Exception as exc:  # noqa: BLE001 - ein unlesbares Buch beendet die Kette, nicht die Zustellung
            log.info("conversation.register_unreadable", kind=type(exc).__name__)
            return ""
        refs = [str(d.get("turn_ref") or "") for d in decisions]
        if turn not in refs:
            return ""
        index = refs.index(turn)
        if index + 1 >= len(decisions):
            return ""
        older = decisions[index + 1]
        return str(older.get("turn_ref") or "") if older.get("outcome") == "clarification" else ""

    def _task_names(self, runtime: Runtime, row: dict[str, Any], task_ids: list[str]) -> str:
        from solvio.specialists.launcher import redact
        ledger = runtime.orchestrator.ledger
        names = []
        for task_id in task_ids:
            try:
                task = ledger.get_task(task_id)
            except Exception:  # noqa: BLE001
                task = None
            if task is not None and task.created_principal == row["principal"]:
                names.append("‚" + redact(str(task.objective))[:60] + "'")
        return " oder ".join(names) if names else "‚…'"

    def _continuation_target(self, runtime: Runtime, row: dict[str, Any],
                             task_id: str, related=()) -> tuple[str, str] | None:
        """Current-chat task or a freshly rechecked, rendered related candidate."""
        ledger = runtime.orchestrator.ledger
        if not task_id.startswith("at-"):
            return None
        links = runtime.store.task_links(row["conversation_id"])
        if task_id not in {link["task_id"] for link in links}:
            from solvio.conversation.related_context import task_entry
            source = next((entry for entry in related if entry.work_id == task_id), None)
            if source is None or task_entry(runtime, row, task_id, source.source_conversation_id) is None:
                return None
        task = ledger.get_task(task_id)
        runs = ledger.runs_for_task(task_id)
        if task is None or task.created_principal != row["principal"] or not runs:
            return None
        return task_id, runs[-1].run_id

    async def _explicit_followup_dispatch(self, row: dict[str, Any], text: str, target: dict,
                                          runtime: Runtime) -> dict[str, Any] | None:
        """`target` im Body: Routing uebersprungen, Folgeanweisung an genau dieses Ziel."""
        from solvio.cognition.types import EscalationEvent
        delivery_id, conversation_id = row["delivery_id"], row["conversation_id"]
        from solvio.agent_runtime import task_revisions as TR
        ok = await asyncio.to_thread(self._target_of_this_chat, runtime, row, target)
        if not ok:
            await self._block(row, runtime, ERROR_FOLLOWUP)
            return None
        turn, kind = await asyncio.to_thread(self._attachment_turn, runtime, row, None)
        if turn:
            # Der explizite Auftragsbezug traegt keinen Anhang (B18-1) — auch keinen aus
            # der offenen Kette (Runde 21, F21-1b): benennen — als `handed_back`, damit
            # keine Kette entsteht (F19-1).
            decision_id = runtime.router.record_text_decision(
                conversation_ref=conversation_id, turn_ref=delivery_id, origin=self._origin(row),
                user_text=text, route_final="", outcome="handed_back", event=EscalationEvent.NONE)
            dispatch = {"action_class": ACTION_ATTACHMENT_UNUSED, "route": "", "scope": "",
                        "attachment_kind": kind, "attachment_turn": turn, "target_task_id": target["task_id"],
                        "target_run_id": target["run_id"], "target_revision": int(target["revision"]),
                        "assistant_text": ATTACHMENT_NEEDS_TASK_TEXT, "decision_id": decision_id, "objective": text,
                        "clarified_turn": "", "routing_invocations": 0, "activity_id": ""}
            await asyncio.to_thread(runtime.store.record_dispatch, delivery_id,
                                    self.worker_generation, _dispatch_json(dispatch))
            return dispatch
        if len(text) > TR.MAX_TEXT:
            # Der Chat nimmt 4000 Zeichen an, eine Folgeanweisung traegt 2000: wie
            # der geroutete Weg (§3.5) wird zurueckgefragt statt still gekuerzt
            # (Review Runde 2, F-2: 3074 Zeichen gesendet, 2000 gebunden).
            decision_id = runtime.router.record_text_decision(
                conversation_ref=conversation_id, turn_ref=delivery_id, origin=self._origin(row),
                user_text=text, route_final="", outcome="clarification", event=EscalationEvent.NONE)
            dispatch = {"action_class": ACTION_TOO_LONG, "route": "", "scope": "",
                        "attachment_kind": _attachment_kind(row), "target_task_id": target["task_id"],
                        "target_run_id": target["run_id"], "target_revision": int(target["revision"]),
                        "assistant_text": TOO_LONG_TEXT, "decision_id": decision_id, "objective": text,
                        "clarified_turn": "", "routing_invocations": 0, "activity_id": ""}
            await asyncio.to_thread(runtime.store.record_dispatch, delivery_id,
                                    self.worker_generation, _dispatch_json(dispatch))
            return dispatch
        decision_id = runtime.router.record_text_decision(
            conversation_ref=conversation_id, turn_ref=delivery_id, origin=self._origin(row),
            user_text=text, route_final="", outcome="dispatched",
            produced_ref=target["task_id"], event=EscalationEvent.NONE)
        dispatch = {"action_class": ACTION_FOLLOWUP, "route": "", "scope": "",
                    "attachment_kind": _attachment_kind(row), "target_task_id": target["task_id"],
                    "target_run_id": target["run_id"], "target_revision": int(target["revision"]),
                    "assistant_text": "", "decision_id": decision_id, "objective": text,
                    "clarified_turn": "", "routing_invocations": 0, "activity_id": ""}
        await asyncio.to_thread(runtime.store.record_dispatch, delivery_id,
                                self.worker_generation, _dispatch_json(dispatch))
        return dispatch

    def _target_of_this_chat(self, runtime: Runtime, row: dict[str, Any], target: dict) -> bool:
        links = runtime.store.task_links(row["conversation_id"])
        if not any(link["task_id"] == target["task_id"] and link["run_id"] == target["run_id"]
                   for link in links):
            return False
        task = runtime.orchestrator.ledger.get_task(target["task_id"])
        return task is not None and task.created_principal == row["principal"]

    # ----------------------------------------------------------------- Frage → Antwort
    async def _answer(self, row: dict[str, Any], text: str, dispatch: dict[str, Any],
                      runtime: Runtime, activity_id: str) -> None:
        from solvio.agent_runtime import personal_context
        from solvio.conversation import answer as AN
        delivery_id, conversation_id = row["delivery_id"], row["conversation_id"]
        activities = self._activities(runtime)
        if not activity_id:
            await self._block(row, runtime, ERROR_RESTARTED)
            return
        routing = int(dispatch.get("routing_invocations", 0) or 0)
        verdict = await asyncio.to_thread(self._orphan_verdict, runtime, activity_id, routing)
        if verdict:
            await self._block(row, runtime, verdict, activity_id=activity_id)
            return
        try:
            binding = await self._prep(asyncio.to_thread(
                activities.binding, activity_id,
                content_digest=content_digest(conversation_id, row["message_id"],
                                              int(row["message_sequence"]), text)))
            store = runtime.store
            context = await self._prep(asyncio.to_thread(
                store.recent_context, conversation_id, before_sequence=int(row["message_sequence"])))
            links = await self._prep(asyncio.to_thread(store.task_links, conversation_id))
            status = await self._prep(asyncio.to_thread(
                AN.status_block, runtime.orchestrator.ledger, links, row["principal"]))
            memory = ""
            if (runtime.personal_memory is not None and runtime.owner_principal
                    and row['principal'] == runtime.owner_principal):
                memory = await self._prep(personal_context.for_call(
                    runtime.personal_memory, query=text, max_chars=2000, purpose="chat_answer"))
            skip = await self._prep(asyncio.to_thread(self._max_ordinal, runtime, activity_id))
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT, activity_id=activity_id)
            return
        except ValueError as exc:
            log.warning("conversation.activity_unusable", delivery_id=delivery_id,
                        reason=str(exc)[:80])
            await self._block(row, runtime, self._unusable_code(runtime, activity_id, exc),
                              activity_id=activity_id)
            return
        related = await self._related_context(row, text, runtime, for_answer=True)
        from solvio.conversation.related_context import answer_status
        related_status = await self._prep(asyncio.to_thread(answer_status, runtime, row, related.entries))
        payload = AN.build_answer_request(text, AN.context_lines(context), status, memory,
                                          route=str(dispatch.get("route") or ""),
                                          related_context=(related.context + "\n" + related_status).strip())
        self.stats["answered"] += 1
        gate = self._source_check(row, runtime)
        try:
            with self._scope(runtime, binding, row) as scope:
                # Ordnungsnummern, die das Routing verbraucht hat, werden gebrannt: die
                # Antwort bekommt nach einem Neustart dieselbe Nummer wie im
                # ununterbrochenen Lauf — und eine schon benutzte trifft im Buch auf
                # ihren Vorgaenger. Das ist die Sperre, keine Luecke.
                for _ in range(skip):
                    scope.next_invocation()
                scope.source_check = gate
                result = await runtime.transport(payload)
        except ValueError as exc:
            log.warning("conversation.activity_unusable", delivery_id=delivery_id,
                        reason=str(exc)[:80])
            await self._block(row, runtime, self._unusable_code(runtime, activity_id, exc),
                              activity_id=activity_id)
            return
        reason = str(result.get("reason") or "")
        if not result.get("ok"):
            code = _error_code(reason or ERROR_PROVIDER)
            if code == ERROR_COST_RECOVERY and gate.revoked:
                # Die Absage kam aus dem Quellen-Check vor dem Claim: Quelle weg (§3.2).
                code, reason = ERROR_SOURCE_REVOKED, ERROR_SOURCE_REVOKED
            await self._block(row, runtime, code, activity_id=activity_id,
                              hold_reason=reason or code)
            return
        if not await self.current(row, runtime):
            await self._block(row, runtime, ERROR_SOURCE_REVOKED, activity_id=activity_id,
                              hold_reason=ERROR_SOURCE_REVOKED)
            return
        # ANTWORTPERSISTENZ ERST NACH GEPRUEFTER KOSTENBINDUNG (§3.2, Muster voice_delegate).
        claim = await asyncio.to_thread(self._claim_row, runtime, result.get("cost_reservation_id"))
        if (claim is None or claim["activity_id"] != activity_id or claim["phase"] != ACTIVITY_PURPOSE
                or claim["state"] != "finished"):
            log.warning("conversation.answer_claim_missing", delivery_id=delivery_id)
            await self._block(row, runtime, ERROR_COST_RECOVERY, activity_id=activity_id)
            return
        answer_text = str(result.get("text") or "").strip()[:AN.MAX_ANSWER_CHARS]
        if not answer_text:
            await self._block(row, runtime, ERROR_OUTPUT, activity_id=activity_id,
                              hold_reason=ERROR_OUTPUT)
            return
        observations = getattr(runtime.orchestrator, 'memory_observations', None)
        learning_pending = bool(observations and observations.adaptive.enabled and not row['attachments']
                                and runtime.owner_principal and row['principal'] == runtime.owner_principal)
        if await self._complete(row, runtime, assistant_text=answer_text, activity_id=activity_id,
                                learning_pending=learning_pending):
            if learning_pending:
                await self._offer_learning(row, runtime)

    def _claim_row(self, runtime: Runtime, reservation_id) -> dict[str, Any] | None:
        if not reservation_id:
            return None
        with runtime.orchestrator.ledger._open() as db:
            row = db.execute("SELECT * FROM agent_provider_invocations WHERE reservation_id=?",
                             (reservation_id,)).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- Auftrag
    def _receipt(self, row: dict[str, Any], *, followup: bool = False):
        from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
        from solvio.capabilities.policy import OriginClass
        principal, reference = row["principal"], str(row["source_ref"] or "")
        if row["source_kind"] == "dashboard":
            session_id = reference[len("browser:"):]
            return (VerifiedTaskReceipt("dashboard_session",
                                        "browser:" + session_id + ":" + row["delivery_id"], principal),
                    OriginClass.TRUSTED_DASHBOARD)
        nonce = reference.rsplit(":", 1)[-1]
        prefix = "app:followup:chat:" if followup else "app:chat:"
        return (VerifiedTaskReceipt("app_session", prefix + nonce, principal),
                OriginClass.TRUSTED_INTERACTIVE_APP)

    async def _start_task(self, row: dict[str, Any], dispatch: dict[str, Any],
                          runtime: Runtime, activity_id: str) -> None:
        """§3.5 — derselbe Aufruf wie `task_endpoint.start`, mit den persistierten Argumenten."""
        from solvio.agent_runtime.task_start_service import AuthorizedTaskStart
        from solvio.capabilities.contract import ArgumentSource
        from solvio.capabilities.envelope import CapabilityOutcome
        from solvio.contracts.trust import TrustContext, TrustLevel
        delivery_id, conversation_id = row["delivery_id"], row["conversation_id"]
        orch = runtime.orchestrator
        scope_name = str(dispatch.get("scope") or DEFAULT_CHAT_TASK_SCOPE)
        capability = "agent_task_" + scope_name
        objective = str(dispatch.get("objective") or "")
        arguments = {"objective": objective}
        source = str(dispatch.get("attachment_turn") or "")
        if source and source != delivery_id:
            # Der Anhang reist aus der Rueckfragekette mit (B20-1); die Zustellung ist
            # persistiert, der Entscheid nennt sie — Replay und Neustart binden dasselbe.
            carrier = await asyncio.to_thread(runtime.store.delivery, conversation_id, source)
            attachments = _load_attachments((carrier or {}).get("attachments", ""))
        else:
            attachments = _load_attachments(row["attachments"])
        receipt, origin = self._receipt(row)
        try:
            start = AuthorizedTaskStart.bind(
                receipt=receipt, request_id=delivery_id, capability=capability, arguments=arguments,
                file_request=attachments if attachments and attachments.get("operation") == "process_files" else None,
                document_request=attachments if attachments and attachments.get("operation") == "extract_text" else None,
                conversation_ref=conversation_id,
                private_data=dispatch.get("private_data") is True and scope_name == "task" and not attachments)
        except ValueError as exc:
            await self._block(row, runtime, "task_start_refused:" + str(exc)[:60], activity_id=activity_id)
            return
        # UNMITTELBAR VOR router.execute (§3.2 c).
        if not await self.current(row, runtime):
            await self._block(row, runtime, ERROR_SOURCE_REVOKED, activity_id=activity_id)
            return
        result = await orch.router.execute(
            capability, arguments, trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
            provenance={key: ArgumentSource.USER_DIRECT for key in arguments},
            principal=row["principal"], origin=origin, task_start=start)
        if result.outcome is not CapabilityOutcome.SUCCESS:
            reason = str(result.reason or result.outcome.value)
            await self._block(row, runtime, "task_start_refused:" + reason[:60], activity_id=activity_id)
            return
        data = result.data if isinstance(result.data, dict) else {}
        task_id, run_id = str(data.get("task_id") or ""), str(data.get("run_id") or "")
        if not task_id or not run_id:
            await self._block(row, runtime, "task_start_refused:no_task", activity_id=activity_id)
            return
        summary = str(data.get("zusammenfassung") or TASK_ACCEPTED_TEXT)
        self._note_produced(runtime, dispatch, task_id)
        completed = await self._complete(row, runtime, assistant_text=summary, activity_id=activity_id,
                                         task_id=task_id, run_id=run_id, revision=1)
        if completed:
            orch.offer_task_observation(task_id, run_id)

    def _note_produced(self, runtime: Runtime, dispatch: dict[str, Any], task_id: str) -> None:
        decision_id = str(dispatch.get("decision_id") or "")
        ledger = getattr(runtime.router, "ledger", None)
        if decision_id and ledger is not None:
            try:
                ledger.set_produced_ref(decision_id, task_id)
            except Exception as exc:  # noqa: BLE001 - ein Buchfehler stoppt nie Arbeit
                log.warning("conversation.produced_ref_failed", kind=type(exc).__name__)

    # ----------------------------------------------------------------- Folgeanweisung
    async def _followup(self, row: dict[str, Any], dispatch: dict[str, Any],
                        runtime: Runtime, activity_id: str) -> None:
        """§3.6 — exakt die Folge aus `task_followup_endpoint`: replay → prepare → current → admit.

        Das Replay steht VOR der Fortsetzbarkeitspruefung (Codex-Review 19.09.2026,
        Befund d): nach einem Absturz zwischen `admit_followup` und dem Endzustand der
        Zustellung ist der Elternlauf nicht mehr fortsetzbar (`followup_active_run`) —
        genau WEIL diese Zustellung den Folgelauf schon angenommen hat. Die
        Wiederaufnahme muss ihn dann wiederfinden und an den Chat binden, nicht
        „laeuft noch" ohne Verweis melden.
        """
        from solvio.agent_runtime import task_revisions as TR
        delivery_id = row["delivery_id"]
        orch = runtime.orchestrator
        ledger = orch.ledger
        task_id, run_id = str(dispatch.get("target_task_id") or ""), str(dispatch.get("target_run_id") or "")
        objective = str(dispatch.get("objective") or "")
        receipt, _ = self._receipt(row, followup=True)
        try:
            revision = await self._prep(asyncio.to_thread(TR.revision_for_run, ledger, run_id))
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT, activity_id=activity_id)
            return
        except (ValueError, TypeError, KeyError, OSError):
            await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
            return
        expected = dispatch.get("target_revision")
        if expected is not None and int(expected) != int(revision["revision"]):
            await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
            return
        value = {"run_id": run_id, "text": objective[:TR.MAX_TEXT].strip(),
                 "expected_revision": int(revision["revision"]),
                 "expected_digest": revision["digest"], "input_artifact_ids": [],
                 "client_request_id": delivery_id}
        try:
            TR.canonical_followup(value)
            prior = await self._prep(asyncio.to_thread(TR.replay, ledger, value, receipt))
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT, activity_id=activity_id)
            return
        except (ValueError, TypeError, KeyError, OSError):
            await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
            return
        if prior is None:
            source = dispatch.get("source_chat")
            if source is not None:
                from solvio.conversation.related_context import task_entry
                valid_source = await self._prep(asyncio.to_thread(task_entry, runtime, row,
                    task_id, str(source.get("conversation_id") or "")))
                if valid_source is None:
                    await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
                    return
            try:
                eligibility = await self._prep(asyncio.to_thread(TR.eligibility, ledger, run_id))
            except asyncio.TimeoutError:
                await self._block(row, runtime, ERROR_TIMEOUT, activity_id=activity_id)
                return
            if not eligibility.get("eligible"):
                run = ledger.get_run(run_id)
                still_running = run is not None and not getattr(run, "terminal", False)
                await self._complete(row, runtime, activity_id=activity_id,
                                     assistant_text=RUNNING_TEXT if still_running else NOT_CONTINUABLE_TEXT)
                return
        try:
            if prior is None:
                prepared = await self._prep(asyncio.to_thread(TR.prepare, ledger, value, receipt))
                # UNMITTELBAR VOR admit_followup (§3.2 c).
                if not await self.current(row, runtime):
                    await self._block(row, runtime, ERROR_SOURCE_REVOKED, activity_id=activity_id)
                    return
                source = dispatch.get("source_chat")
                if source is not None:
                    from solvio.conversation.related_context import task_entry
                    if await asyncio.to_thread(task_entry, runtime, row, task_id,
                            str(source.get("conversation_id") or "")) is None:
                        await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
                        return
                _, run = orch.task_starts.admit_followup(prepared)
                admitted = TR.revision_for_run(ledger, run.run_id)
            else:
                admitted = prior
        except asyncio.TimeoutError:
            await self._block(row, runtime, ERROR_TIMEOUT, activity_id=activity_id)
            return
        except (ValueError, TypeError, KeyError, OSError):
            try:
                admitted = TR.replay(ledger, value, receipt)
            except (ValueError, TypeError, KeyError, OSError):
                admitted = None
            if admitted is None:
                await self._block(row, runtime, ERROR_FOLLOWUP, activity_id=activity_id)
                return
        self._note_produced(runtime, dispatch, task_id)
        source = dispatch.get("source_chat")
        accepted_text = FOLLOWUP_ACCEPTED_TEXT
        if isinstance(source, dict) and source.get("title"):
            accepted_text = f"Ich setze den Auftrag aus dem Chat ‚{source['title']}' hier fort."
        completed = await self._complete(
            row, runtime, activity_id=activity_id, assistant_text=accepted_text,
            task_id=str(admitted["task_id"]), run_id=str(admitted["run_id"]),
            revision=int(admitted["revision"]))
        if completed:
            orch.offer_task_observation(str(admitted["task_id"]), str(admitted["run_id"]))

    # ----------------------------------------------------------------- Endzustaende
    # Die Aktivitaet wird VOR dem Endzustand der Zustellung geschlossen: der Endzustand
    # ist damit der letzte Schreibvorgang, und wer `completed`/`blocked` liest, sieht
    # eine geschlossene Aktivitaet (§1.6: keine terminale Zustellung mit offener
    # Aktivitaet). Ein Absturz dazwischen laesst eine `running`-Zeile mit geschlossener
    # Aktivitaet zurueck — die Wiederaufnahme liest den Claim-Befund und wiederholt
    # keinen physischen Lauf.

    async def _complete(self, row: dict[str, Any], runtime: Runtime, *, assistant_text: str,
                        activity_id: str = "", task_id: str = "", run_id: str = "",
                        revision: int = 0, learning_pending: bool = False) -> bool:
        delivery_id = row["delivery_id"]
        if activity_id:
            try:
                await asyncio.to_thread(self._activities(runtime).finish, activity_id)
            except Exception as exc:  # noqa: BLE001 - die Zustellung endet trotzdem
                log.warning("conversation.activity_finish_failed", kind=type(exc).__name__)
        try:
            await asyncio.to_thread(runtime.store.complete_delivery, delivery_id,
                                    self.worker_generation, assistant_text=assistant_text,
                                    task_id=task_id, run_id=run_id, revision=revision,
                                    learning_pending=learning_pending)
        except ConversationStoreError as exc:
            if "delivery_lost" in str(exc):
                self.stats["lost"] += 1
                log.warning("conversation.delivery_lost", delivery_id=delivery_id, stage="complete")
                return False
            raise
        self.stats["completed"] += 1
        log.info("conversation.delivery_completed", delivery_id=delivery_id,
                 task_id=task_id or None, revision=revision or None)
        return True

    async def _block(self, row: dict[str, Any], runtime: Runtime, error_code: str, *,
                     activity_id: str = "", hold_reason: str = "") -> bool:
        """`blocked` — und die Aktivitaet wird IN JEDEM FALL geschlossen (§3.4, §1.6)."""
        delivery_id = row["delivery_id"]
        if activity_id:
            activities = self._activities(runtime)
            try:
                if hold_reason and error_code != ERROR_COST_RECOVERY:
                    await asyncio.to_thread(activities.hold, activity_id, hold_reason[:120])
                await asyncio.to_thread(activities.finish, activity_id, cancelled=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("conversation.activity_close_failed", kind=type(exc).__name__)
        try:
            await asyncio.to_thread(runtime.store.block_delivery, delivery_id,
                                    self.worker_generation, error_code=error_code)
        except ConversationStoreError as exc:
            if "delivery_lost" in str(exc):
                self.stats["lost"] += 1
                log.warning("conversation.delivery_lost", delivery_id=delivery_id, stage="block")
                return False
            raise
        self.stats["blocked"] += 1
        log.warning("conversation.delivery_blocked", delivery_id=delivery_id, error_code=error_code)
        return True


# --------------------------------------------------------------------- Helfer
def _attachment_kind(row: dict[str, Any]) -> str:
    attachments = _load_attachments(row.get("attachments", ""))
    if not attachments:
        return ""
    return "file" if attachments.get("operation") == "process_files" else "document"


def _load_attachments(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _load_target(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {"task_id", "run_id", "revision"}:
        return None
    return value


def transport_from_settings():
    """Der Abo-Transport des Textwegs (Entscheidung 1 in §10.4): EINE Zeile ist der
    Umschaltpunkt zum Broker-Weg."""
    from solvio.specialists.subscription import SubscriptionTransport
    try:
        from solvio.config import load_settings
        settings = load_settings()
        provider = getattr(settings, "agent_runtime_subscription_provider", "codex") or "codex"
        model = getattr(settings, "agent_runtime_subscription_model", "") or ""
    except Exception:  # noqa: BLE001 - ohne Einstellungen der Standardweg
        provider, model = "codex", ""
    return SubscriptionTransport(provider, model=model)
