"""Ein sehr schmaler Griff in den laufenden Core — fuer den Besitzer, nicht fuers Modell.

Der Anlass ist eine Altlast, die ein echter Portal-Lauf sichtbar gemacht hat: der
Freigabeweg lebt im Core-Prozess, weil die bestaetigte Entscheidung dort im
Arbeitsspeicher liegt. Wer eine freigabepflichtige Faehigkeit ausfuehren will,
muss also **im Core** sein. Ein Abnahmeskript daneben konnte das nicht — es hat
stattdessen den Core angehalten und selbst Port 8770 belegt.

Das ist die falsche Richtung. Zwischen zwei Laeufen lag dann gar kein Gateway,
und die iPhone-App, die nur beim Nachfragen etwas erfaehrt, fand nichts. Vier
gescheiterte Abnahmeversuche gingen darauf zurueck.

Also bekommt der Core einen Griff statt eines Konkurrenten. Bewusst so klein wie
moeglich:

* **Ein Unix-Socket**, kein Port. Ein lauschender Port ist fuer jeden lokalen
  Prozess erreichbar und kennt seinen Anrufer nicht; ein Unix-Socket kennt ihn,
  vom Kern, beim `connect`, nicht auf Zuruf. Dieselbe Naht wie zum Portal-
  Arbeiter, dieselbe Pruefung.
* **Fuenf Vorgaenge**: Zustand melden, eine benannte Faehigkeit ausfuehren, den
  Stand einer Freigabe NACHLESEN, eine Notiz wiederfinden und einen
  Agentenauftrag wiederfinden. Kein Code, kein Pfad, kein Werkzeugname
  ausser den registrierten. Das Nachlesen fuehrt nichts aus — es gab bisher nur
  den Ausfuehrungsaufruf als Auskunft, und der beantwortete „abgelaufen" wie
  „erledigt" mit demselben `not_approved`.
* **Keine neue Autoritaet.** Der Anrufer ist der Besitzer des Rechners — er
  koennte ohnehin alles lesen. Was er hier *nicht* bekommt, ist eine Abkuerzung
  an der Freigabe vorbei: eine kritische Faehigkeit endet auch hier bei
  `approval_required` und braucht das iPhone.

Wer das liest und an eine Fernsteuerung denkt: es gibt keinen Weg von hier zu
einem beliebigen Kommando. Die Fähigkeiten sind die, die auch die Stimme hat.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from typing import Any

from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.logging_setup import get_logger
from solvio.portal import protocol as P

log = get_logger("core")

#: Wo der Griff liegt. Im Zustandsverzeichnis des Besitzers, nicht in /tmp.
DEFAULT_SOCKET = os.path.expanduser("~/.solvio/control.sock")

#: Dieselbe Variable, mit der der Core seinen Griff schon heute verlegt
#: (`core_server`, `SOLVIO_CONTROL_SOCKET_PATH`) — jetzt folgt ihr auch der
#: Standard dieses Moduls, fuer Server und Client (DEBT-0223). In Produktion
#: ungesetzt: dann gilt `DEFAULT_SOCKET` wie bisher.
SOCKET_ENV = "SOLVIO_CONTROL_SOCKET_PATH"


def default_socket() -> str:
    return os.path.expanduser(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)

#: Der Principal, unter dem ein oertlicher Aufruf laeuft. Ausdruecklich nicht der
#: eines Satelliten: wer hier fragt, sitzt am Rechner.
CONTROL_PRINCIPAL = "local-control"

HEALTH = "health"
RUN = "run_capability"

#: Reine Auskunft ueber eine Freigabe. Fuehrt nichts aus und legt nichts an.
APPROVAL_STATUS = "approval_status"

#: Ein Broker-Token fuer den Entwicklungs-Autopiloten praegen.
#:
#: Warum es diese Operation gibt: die Broker-Registratur lebt im
#: Core-Prozess, und ihre Tageskappen sind nur dort EINE Wahrheit. Der
#: Autopilot-Treiber laeuft aber bewusst als eigener Prozess — Baulaeufe
#: dauern Stunden und gehoeren nicht in den Core. Ohne diese Naht bliebe nur
#: eine zweite Broker-Instanz mit eigenen Kappen, und zwei Kappen sind keine
#: Kappe.
#:
#: Was hier NICHT herausgeht: ein Anbieterschluessel. Ein Broker-Token oeffnet
#: ohne offenes Lease nichts, gilt nur fuer seinen Auftraggeber und faellt
#: unter dessen Kappen. Der Anrufer ist ueber die Kennung des Sockets als der
#: Besitzer belegt — kein Modell, kein Kaefig und kein Satellit erreicht ihn.
AUTOPILOT_TOKEN = "autopilot_token"
AUTOPILOT_LEASE = "autopilot_lease"

#: Eine Notiz im naechsten Gespraech wiederfinden — ebenfalls nur lesend.
NOTE_STATUS = "note_status"

#: Einen Agentenauftrag im naechsten Gespraech wiederfinden — nur lesend.
#: Deckt auch den Zustand VOR dem Lauf: eine Startfreigabe, die offen,
#: abgelehnt, abgelaufen oder im Ausgang ungewiss ist. Bei mehreren passenden
#: Auftraegen waehlt er keinen.
TASK_STATUS = "agent_task_status"

# Einmalanmeldung/Widerruf nur am bereits uid-geprueften lokalen Socket.
# Kein Modellwerkzeug und kein oeffentlicher HTTP-Aussteller.
BROWSER_ENROLL = "browser_session_enroll"
BROWSER_REVOKE = "browser_session_revoke"

#: **Die Liste ist die Tuer.** `handle` weist alles ab, was hier nicht steht.
#: Eine neue Operation nur unten einzuhaengen genuegt nicht: sie kam nie an,
#: und ein Test, der die Methode direkt rief, hat es nicht gemerkt.
OPERATIONS = frozenset({HEALTH, RUN, AUTOPILOT_TOKEN, AUTOPILOT_LEASE,
                        APPROVAL_STATUS, NOTE_STATUS, TASK_STATUS,
                        BROWSER_ENROLL, BROWSER_REVOKE})

#: Was der Lease-Vorgang tun darf. Zwei Woerter, keine Zeichenkette aus dem
#: Aufruf — sonst waere er eine Fernbedienung fuer die Registratur.
LEASE_ACTIONS = ("open", "close")

#: Wie lange ein Lease des Autopiloten hoechstens offen bleibt. Der Anrufer
#: darf das nicht setzen: eine Frist, die der Bittsteller bestimmt, ist keine.
LEASE_SECONDS = 300.0

#: Genau die zwei Auftraggeber des Autopiloten. Eine Liste, keine Zeichenkette
#: aus dem Aufruf: sonst waere die Operation ein Token-Automat fuer beliebige
#: Namen.
AUTOPILOT_PRINCIPALS = ("autopilot-lead", "autopilot-lead-escalation",
                        # Der schreibende Claude-Builder und seine
                        # Eskalationsstufe (V0.6). Sie sprechen die
                        # Anthropic-Flaeche; ihr Token oeffnet dort nichts
                        # ohne Lease und ausserhalb der Rueckschleife gar
                        # nichts.
                        "autopilot-writer-claude",
                        "autopilot-writer-claude-escalation")

CALL_TIMEOUT = 300.0


class CoreControl:
    """Nimmt oertliche Auftraege an — und nur vom Besitzer."""

    def __init__(self, dispatcher: Any, *, socket_path: str | None = None,
                 owner_uid: int | None = None) -> None:
        self.dispatcher = dispatcher
        self.socket_path = default_socket() if socket_path is None else socket_path
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._server: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._turn = 0
        self._stopping = False
        self._connections: set[socket.socket] = set()
        self._conversations: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._stopping = False
        directory = os.path.dirname(self.socket_path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # 0600: nur der Besitzer. Es gibt keine Gruppe, die hier mitreden soll.
        self._server = P.bind_listener(self.socket_path, mode=0o600)
        self._server.setblocking(False)
        self._task = asyncio.create_task(self._serve())
        log.info("core.control_listening", socket=self.socket_path,
                 owner_uid=self.owner_uid)

    async def stop(self) -> None:
        self._stopping = True
        accept, self._task = self._task, None
        if self._server is not None:
            self._server.close()
            self._server = None
        if accept is not None:
            accept.cancel()
        # P.decode runs in the default executor. Cancellation alone cannot
        # interrupt its blocking recv; shutdown wakes it before draining tasks.
        for connection in tuple(self._connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        jobs = set(self._conversations)
        if accept is not None:
            jobs.add(accept)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._connections.clear()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, _ = await loop.sock_accept(self._server)
            except (asyncio.CancelledError, OSError):
                return
            if self._stopping:
                connection.close()
                return
            self._connections.add(connection)
            job = asyncio.create_task(self._converse(connection))
            self._conversations.add(job)
            job.add_done_callback(self._conversations.discard)

    async def _converse(self, connection: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Die Kennung zuerst, vor dem ersten Byte der Anfrage.
            P.authenticate(connection, allowed_uid=self.owner_uid)
        except P.ProtocolError as exc:
            log.warning("core.control_caller_rejected", detail=str(exc)[:80])
            connection.close()
            self._connections.discard(connection)
            return
        connection.setblocking(False)
        try:
            while not self._stopping:
                message = await loop.run_in_executor(None, P.decode,
                                                     _Blocking(connection))
                reply = await self.handle(message)
                await loop.sock_sendall(connection, P.encode(reply))
        except (P.ProtocolError, OSError):
            pass
        finally:
            connection.close()
            self._connections.discard(connection)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        if self._stopping:
            return P.failure("core_stopping")
        operation = str(message.get("op", ""))
        if operation not in OPERATIONS:
            return P.failure("unknown_operation", operation[:40])
        try:
            if operation == HEALTH:
                return await self._health()
            if operation == AUTOPILOT_TOKEN:
                return await self._autopilot_token(message)
            if operation == AUTOPILOT_LEASE:
                return await self._autopilot_lease(message)
            if operation == APPROVAL_STATUS:
                return await self._approval_status(message)
            if operation == NOTE_STATUS:
                return await self._note_status(message)
            if operation == TASK_STATUS:
                return await self._task_status(message)
            if operation in {BROWSER_ENROLL, BROWSER_REVOKE}:
                return await self._browser_session(message)
            return await self._run(message)
        except Exception as exc:  # noqa: BLE001 - ein Auftrag reisst den Core nie mit
            log.error("core.control_failed", op=operation, kind=type(exc).__name__)
            return P.failure("control_failed", type(exc).__name__)

    async def _browser_session(self, message):
        runtime = getattr(self.dispatcher, "approver_runtime", None)
        sessions = getattr(runtime, "browser_sessions", None)
        principal = getattr(getattr(runtime, "approvals", None), "owner_principal", "")
        if sessions is None or not principal:
            return P.failure("browser_sessions_unavailable")
        if message["op"] == BROWSER_ENROLL:
            from solvio.security.mobile_approval import observer_contract as O
            if set(message) == {"op", "purpose"} and message["purpose"] == O.PURPOSE:
                from urllib.parse import urlsplit
                def current_binding():
                    plane = getattr(runtime, "control_plane", None)
                    origin = getattr(sessions, "observer_origin", "")
                    binding = O.Binding(getattr(plane, "core_instance_id", ""), principal,
                                        origin, getattr(runtime, "tls_fingerprint", ""))
                    if (getattr(self.dispatcher, "approver_runtime", None) is not runtime
                            or getattr(runtime, "browser_sessions", None) is not sessions
                            or getattr(runtime.approvals, "owner_principal", "") != principal
                            or sessions.core_instance_id != binding.core_instance_id
                            or sessions.observer_tls_fingerprint != binding.tls_fingerprint
                            or urlsplit(origin).hostname not in getattr(runtime, "bound_hosts", ())
                            or urlsplit(origin).port != getattr(runtime, "port", None)):
                        raise ValueError("observer_runtime_changed")
                    return binding
                try:
                    expected = current_binding()
                    enrollment = await sessions.issue_observer_enrollment(principal=principal)
                    if current_binding() != expected or enrollment.binding != expected:
                        raise ValueError("observer_runtime_changed")
                except (ValueError, AttributeError):
                    return P.failure("observer_unavailable")
                return enrollment.as_response()
            if set(message) != {"op"}:
                return P.failure("invalid_browser_enrollment")
            enrollment = await sessions.issue_enrollment(principal=principal)
            # Nur die direkte 0600-Socketantwort; kein Log/URL/Modellkontext.
            return {"ok": True, "token": enrollment.token, "expires_at": enrollment.expires_at}
        if set(message) != {"op", "session_id"} or not isinstance(message["session_id"], str):
            return P.failure("invalid_browser_session")
        revoked = await sessions.revoke(message["session_id"], principal=principal)
        return {"ok": True, "revoked": revoked}

    async def _health(self) -> dict[str, Any]:
        """Was ein Abnahmelauf wissen muss, bevor er ueberhaupt anfaengt.

        Vor allem `approver`: ohne Freigabeweg endet jede kritische Faehigkeit
        bei `approval_required`, und ein Lauf, der das erst nach dem Absenden
        merkt, hat eine Wartende Freigabe erzeugt, die niemand einloest.
        """
        approver = getattr(self.dispatcher, "approver_runtime", None)
        voice_server = getattr(approver, "voice_server", None)
        pending: list[Any] = []
        if approver is not None:
            try:
                pending = await approver.approvals.pending()
            except Exception:  # noqa: BLE001 - Zustand melden, nicht scheitern
                pending = []
        return {"ok": True, "pid": os.getpid(),
                "capabilities": self.dispatcher.capabilities.names(),
                "approver": approver is not None,
                "gateway_port": getattr(approver, "port", 0) if approver else 0,
                "pending": len(pending),
                "portal": getattr(self.dispatcher, "portal", None) is not None,
                "voice": {"model": getattr(voice_server, "model", None),
                          "delegate_ready": getattr(self.dispatcher, "live_backend", None) is not None}}

    async def _note_status(self, message: dict[str, Any]) -> dict[str, Any]:
        """**Eine Notiz wiederfinden — nur lesend, und niemals anlegen.**

        Der Anlass: ein neues Gespraech kennt die alte Freigabekennung nicht.
        Ueber `notieren(text)` nachzufragen waere kein Statusweg, sondern ein
        neuer Auftrag — im Zweifel eine zweite Notiz.

        Gesucht wird im dauerhaften Merkzettel des Cores, dem einzigen Ort, an
        dem Wortlaut und Kennung zusammenstehen. Ist die Zuordnung nicht
        eindeutig, wird KEINE gewaehlt: dann kommen die Wortlaute zurueck und
        der Mensch entscheidet.
        """
        gesucht = str(message.get("text") or "").strip()
        ledger = getattr(getattr(self.dispatcher, "agent_runtime", None),
                         "ledger", None)
        if ledger is None:
            return P.failure("no_agent_ledger")
        try:
            zettel = ledger.starts_for_capability("note_write", limit=20)
        except Exception as exc:  # noqa: BLE001
            return P.failure("ledger_unreadable", type(exc).__name__)
        if not zettel:
            return {"ok": True, "found": False, "ambiguous": False,
                    "candidates": []}

        def _text(eintrag: dict) -> str:
            return str((eintrag.get("arguments") or {}).get("text") or "").strip()

        if gesucht:
            treffer = [z for z in zettel if _text(z) == gesucht]
            if not treffer:
                # Kein exakter Wortlaut: was es gibt, wird GENANNT, nicht geraten.
                return {"ok": True, "found": False, "ambiguous": False,
                        "candidates": [_text(z) for z in zettel[:5] if _text(z)]}
        else:
            treffer = zettel
        if len(treffer) > 1 and not gesucht:
            # Ohne Wortlaut und mit mehreren Auftraegen wird nichts gewaehlt.
            return {"ok": True, "found": False, "ambiguous": True,
                    "candidates": [_text(z) for z in treffer[:5] if _text(z)]}

        eintrag = treffer[0]
        stand = await self._approval_status(
            {"approval_request_id": eintrag["request_id"]})
        if not stand.get("ok"):
            return stand
        stand.update({"found": True, "ambiguous": False,
                      "text": _text(eintrag),
                      "request_id": eintrag["request_id"],
                      "start_state": str(eintrag.get("state") or "")})
        return stand

    async def _task_status(self, message: dict[str, Any]) -> dict[str, Any]:
        """**Einen Agentenauftrag wiederfinden — nur lesend, ohne zu raten.**

        Der Anlass ist derselbe wie bei der Notiz, nur groesser: ein Auftrag
        laeuft ueber das Gespraechsende hinaus, und die naechste Instanz kennt
        weder Lauf- noch Freigabekennung. Ueber `agent_run_status` liesse sich
        nur ein Lauf mit bekannter Kennung lesen — und VOR der ersten Freigabe
        gibt es gar keinen Lauf, nur den Merkzettel.

        Gesucht wird in der Auftragsauskunft (`agent_runtime.inquiry`): die
        juengsten Laeufe samt Wortlaut, Grenze und Belegen, dazu die
        Startfreigaben ohne verzeichneten Lauf. `text` ist Wortlaut oder Teil
        davon, `key` eine Kennung, die dieses Gespraech vom Core bekam. Bei
        mehreren passenden Auftraegen wird KEINER gewaehlt.

        Der Freigabezustand kommt aus `_approval_status` — dieselbe Lesung wie
        fuer eine Notiz, kein zweiter Leser.
        """
        text = str(message.get("text") or "").strip()
        key = str(message.get("key") or "").strip()
        ledger = getattr(getattr(self.dispatcher, "agent_runtime", None),
                         "ledger", None)
        if ledger is None:
            return P.failure("no_agent_ledger")
        from solvio.agent_runtime import inquiry

        async def freigabe(request_id: str) -> dict[str, Any]:
            return await self._approval_status({"approval_request_id": request_id})

        return await inquiry.find(ledger, text=text, key=key,
                                  approval_status=freigabe)

    async def _autopilot_token(self, message: dict[str, Any]) -> dict[str, Any]:
        """Praegt einen Broker-Token fuer EINEN der zwei Autopilot-Auftraggeber.

        Die Namensliste ist geschlossen. Waere der Name frei waehlbar, koennte
        sich der Anrufer den Token des Eskalations-Auftraggebers eines anderen
        Milestones praegen — oder den des Deep-Gateways.
        """
        name = str(message.get("principal", ""))
        if name not in AUTOPILOT_PRINCIPALS:
            return P.failure("unknown_principal", name[:40])
        broker = getattr(self.dispatcher, "provider_broker", None)
        if broker is None:
            return P.failure("broker_unavailable", "")
        try:
            token = broker.register_principal(name)
        except Exception as exc:  # noqa: BLE001 - der Core reisst hier nicht ab
            log.error("core.autopilot_token_failed", kind=type(exc).__name__)
            return P.failure("mint_failed", type(exc).__name__)
        log.info("core.autopilot_token_minted", principal=name)
        return {"ok": True, "principal": name, "token": token}

    async def _autopilot_lease(self, message: dict[str, Any]) -> dict[str, Any]:
        """Oeffnet oder schliesst EIN Lease fuer einen der zwei Auftraggeber.

        Warum es das gibt: ein Broker-Token allein oeffnet nichts — ohne
        offenes Lease antwortet der Broker mit 403 `lease_absent`. Die
        Registratur lebt im Core, der Treiber aber laeuft als eigener Prozess.
        Ohne diesen Vorgang muesste er sich einen ZWEITEN Broker starten, und
        zwei Kappen sind keine Kappe: die Tageskappe des Anbieters waere
        doppelt vergeben.

        Was er ausdruecklich NICHT kann: einen fremden Auftraggeber bedienen
        (die Liste ist dieselbe geschlossene wie beim Token), eine eigene
        Frist setzen, oder ein fremdes Lease schliessen — `close` gibt nur
        weiter, was `open` vorher zurueckgegeben hat, und der Broker prueft
        die Kennung selbst.
        """
        name = str(message.get("principal", ""))
        if name not in AUTOPILOT_PRINCIPALS:
            return P.failure("unknown_principal", name[:40])
        aktion = str(message.get("action", ""))
        if aktion not in LEASE_ACTIONS:
            return P.failure("unknown_lease_action", aktion[:40])
        broker = getattr(self.dispatcher, "provider_broker", None)
        if broker is None:
            return P.failure("broker_unavailable", "")
        try:
            if aktion == "open":
                lease = broker.open_lease(
                    name, ref=f"autopilot:{name}",
                    deadline=time.time() + LEASE_SECONDS)
                log.info("core.autopilot_lease_opened", principal=name)
                return {"ok": True, "principal": name, "lease_id": lease}
            broker.close_lease(str(message.get("lease_id", "")))
            log.info("core.autopilot_lease_closed", principal=name)
            return {"ok": True, "principal": name}
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("core.autopilot_lease_refused", principal=name,
                     action=aktion, kind=type(exc).__name__)
            return P.failure("lease_refused", type(exc).__name__)

    async def _run(self, message: dict[str, Any]) -> dict[str, Any]:
        """Fuehrt eine registrierte Faehigkeit im laufenden Core aus.

        Der ganze Sinn: die Freigabe laeuft ueber den Gateway, den dieser Prozess
        ohnehin bedient. Niemand muss den Core dafuer anhalten, und zwischen zwei
        Aufrufen bleibt das iPhone erreichbar.
        """
        name = str(message.get("capability", ""))
        arguments = message.get("arguments") or {}
        approval = message.get("approval_request_id") or None
        if not isinstance(arguments, dict):
            return P.failure("invalid_arguments")

        gate = self.dispatcher.capability_gate
        self._turn += 1
        # Wer am Rechner sitzt, ist der Besitzer — der Kern hat seine uid beim
        # `connect` bestaetigt. Das ist dieselbe Autoritaet wie eine gesprochene
        # Bitte, und genau wie dort entscheidet ueber eine kritische Aktion
        # weiterhin das iPhone, nicht dieser Socket.
        #
        # Die Notiz sagt ausdruecklich, woher der Turn kommt. `voice_trust()`
        # waere bequemer gewesen, haette aber „authenticated voice turn" ins
        # Journal geschrieben — und ein Journal, das die Herkunft beschoenigt,
        # ist genau dann wertlos, wenn man es braucht.
        gate.begin_turn(session_id="control", turn_id=f"c-{self._turn}",
                        principal=CONTROL_PRINCIPAL,
                        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                           user_authorized=True,
                                           note="local control socket, owner uid verified"),
                        user_text=f"Lokaler Aufruf: {name}",
                        # Der Socket beweist eine uid, keinen Menschen: JEDER
                        # Prozess unter diesem Konto erreicht ihn. Deshalb eine
                        # eigene Herkunft, die die iPhone-Zeile NICHT erbt.
                        origin=OriginClass.LOCAL_OWNER,
                        # Ein Aufruf ueber den Socket IST der Auftrag —
                        # hier gibt es keine Aeusserung zu deuten.
                        commanded=True)
        context = gate.context()
        result = await self.dispatcher.capabilities.execute(
            name, arguments, trust=context.trust,
            provenance=gate.provenance_for(arguments),
            principal=context.principal, approval_request_id=approval,
            origin=context.origin, commanded=context.commanded)
        # **Der Merkzettel — derselbe, den der Werkzeugadapter benutzt.**
        #
        # Ohne ihn endete jede freigabepflichtige Faehigkeit ueber diesen Socket
        # in der Luft: die Anfragekennung stand im Umschlag des Aufrufers und
        # starb mit ihm. Gab der Owner spaeter frei, fuehrte niemand aus und
        # niemand benachrichtigte. Genau diese Luecke beschreibt der Kommentar
        # in `remember_pending_start` als schwersten Fund des
        # Agent-Runtime-Milestones — sie galt bisher nur nicht fuer diesen Weg.
        #
        # Es ist KEINE zweite Fortsetzungslogik: Poller, `pending_starts` und
        # Benachrichtigung bleiben, wo sie sind. Hier wird nur der Zettel
        # gelegt, mit Argumenten, Prinzipal und Herkunft dieses Aufrufs.
        try:
            from solvio.tools.agent_capability_tools import remember_pending_start
            ledger = getattr(getattr(self.dispatcher, "agent_runtime", None),
                             "ledger", None)
            remember_pending_start(ledger, capability=name, result=result,
                                   arguments=arguments, context=context)
        except Exception as exc:  # noqa: BLE001 - ein Zettel reisst nie den Aufruf mit
            log.warning("core.control_remember_failed", kind=type(exc).__name__)
        return {"ok": result.succeeded, "outcome": result.outcome.value,
                "reason": result.reason, "message": result.human_message,
                "data": result.data}

    async def _approval_status(self, message: dict[str, Any]) -> dict[str, Any]:
        """**Nur lesen.** Zustand einer Freigabe und ihr Ausfuehrungsbeleg.

        Der Anlass: wer den Zustand ueber `run_capability` erfragt, FUEHRT AUS,
        sobald freigegeben ist — und bekommt fuer EXPIRED wie fuer CONSUMED
        dasselbe `not_approved` zurueck. „Abgelaufen" und „laengst erledigt"
        sahen damit aus wie „wartet noch".

        Hier wird nichts ausgefuehrt und nichts angelegt: `get_request` und
        `attempts_for` sind beide lesend. Zurueck geht der Zustand, nicht der
        Inhalt — keine Argumente, kein Notiztext, keine Details.
        """
        approval_id = str(message.get("approval_request_id") or "")
        if not approval_id:
            return P.failure("missing_approval_request_id")
        approver = getattr(self.dispatcher, "approver_runtime", None)
        plane = getattr(approver, "control_plane", None)
        store = getattr(plane, "store", None)
        if store is None:
            return P.failure("no_approval_store")
        # **Dieselbe Lesefunktion, die auch der Poller benutzt.** Sie
        # behandelt eine abgelaufene PENDING-Zeile als EXPIRED und eine
        # unbekannte Kennung ebenfalls als EXPIRED, nicht als Ablehnung — ein
        # Leseproblem ist kein Nein. Eine zweite Fassung davon waere eine
        # zweite Wahrheit ueber dieselbe Frage.
        from solvio.agent_runtime import steps as ST
        zustand = await ST.read_approval_state(plane, approval_id)
        request = await store.get_request(approval_id)
        if request is None:
            return {"ok": True, "known": False, "state": zustand,
                    "capability": "", "executed": False, "uncertain": False,
                    "attempts": []}
        from solvio.security.mobile_approval import execution as X
        execution_id = X.execution_id_for(plane.core_instance_id, approval_id)
        versuche = await store.attempts_for(execution_id)
        zustaende = [str(v.get("status") or "") for v in versuche]
        return {
            "ok": True, "known": True,
            "state": zustand,
            "capability": str(request.get("tool") or ""),
            # **Erfolg kommt aus dem Beleg, nicht aus dem Zustand allein.**
            "executed": X.SUCCEEDED in zustaende,
            # Der dauerhafte Rand wurde ueberschritten und der Ausgang ist
            # offen — die Wirkung KANN eingetreten sein.
            "uncertain": (X.UNKNOWN in zustaende) and (X.SUCCEEDED not in zustaende),
            "attempts": zustaende,
        }


class _Blocking:
    """Blockierender Blick auf einen nicht-blockierenden Socket (wie beim Portal)."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def recv(self, count: int) -> bytes:
        self._connection.setblocking(True)
        try:
            return self._connection.recv(count)
        finally:
            self._connection.setblocking(False)


class ControlClient:
    """Die andere Seite — fuer Abnahmelaeufe und `solvio`-Werkzeuge."""

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = default_socket() if socket_path is None else socket_path

    def available(self) -> bool:
        return os.path.exists(self.socket_path)

    async def call(self, message: dict[str, Any], *,
                   timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        def exchange() -> dict[str, Any]:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(self.socket_path)
            try:
                connection.sendall(P.encode(message))
                return P.decode(connection)
            finally:
                connection.close()

        return await asyncio.wait_for(loop.run_in_executor(None, exchange),
                                      timeout=timeout + 5)

    async def health(self) -> dict[str, Any]:
        return await self.call({"op": HEALTH}, timeout=15)

    async def observer_enrollment(self, *, expected=None):
        """Fixed local handoff only. Never redeem an old full-owner response."""
        import stat
        from solvio.security.mobile_approval import observer_contract as O
        path = self.socket_path
        native = os.lstat(path)
        directory = os.lstat(os.path.dirname(path))
        if (not os.path.isabs(path) or os.path.realpath(path) != path
                or not stat.S_ISSOCK(native.st_mode) or native.st_uid != os.getuid()
                or stat.S_IMODE(native.st_mode) != 0o600
                or not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            raise ValueError("invalid_observer_control_socket")
        reply = await self.call({"op": BROWSER_ENROLL, "purpose": O.PURPOSE}, timeout=15)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino, after.st_uid, after.st_mode) != (
                native.st_dev, native.st_ino, native.st_uid, native.st_mode):
            raise ValueError("observer_control_socket_changed")
        return O.validate_enrollment(reply, expected=expected)

    async def run(self, capability: str, arguments: dict[str, Any] | None = None, *,
                  approval_request_id: str = "",
                  timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        return await self.call({"op": RUN, "capability": capability,
                                "arguments": arguments or {},
                                "approval_request_id": approval_request_id},
                               timeout=timeout)

    async def task_status(self, text: str = "", key: str = "", *,
                          timeout: float = 30.0) -> dict[str, Any]:
        """Einen Agentenauftrag wiederfinden — nur lesend."""
        return await self.call({"op": TASK_STATUS, "text": text, "key": key},
                               timeout=timeout)
