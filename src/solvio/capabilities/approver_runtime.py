"""Der produktive Freigabeweg im Core-Prozess.

Bis hierher war die iPhone-Kontrollebene ein eigenstaendiges Bring-up-Skript, das
ausdruecklich „nicht in den laufenden Core eingehaengt" war. Damit eine
Faehigkeit tatsaechlich auf eine Freigabe warten und danach weiterlaufen kann,
muss sich das aendern — und zwar **in einem Prozess**.

Der Grund ist keine Bequemlichkeit. `MobileApprover` haelt die bestaetigte
Entscheidung im Arbeitsspeicher: die Verifikation der Geraeteentscheidung legt sie
dort ab, und die Ausfuehrung liest sie von dort wieder. Laufen Gateway und Core
getrennt, sieht die Ausfuehrung die Bestaetigung nie — die Freigabe waere erteilt
und trotzdem wirkungslos. Zwei Prozesse an einer Datei koennen Zustand teilen, ein
Prozessspeicher nicht.

Gebaut wird nichts Neues: Kontrollebene, Broker, Approver, Koordinator und die
HTTP-Fläche stammen unveraendert aus dem eingefrorenen Sicherheitspfad. Dieses
Modul stellt sie nur zusammen und bindet sie an eine LAN-Adresse.

Fail-closed an jeder Stelle: ohne ausdruecklichen Laufzeitmodus, ohne echten
App-Attest-Pruefer, ohne Zustandsverzeichnis startet der Freigabeweg nicht —
und dann gibt es eben keine wirksamen Schreibvorgaenge.
"""
from __future__ import annotations

import os
import ssl
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("approvals")

#: Der Principal, auf den das iPhone registriert ist. Wer fragt und wer freigeben
#: darf, sind zwei Rollen — die Kontrollebene weist eine Entscheidung zurueck,
#: wenn Geraet und Anfrage nicht denselben Principal tragen.
OWNER_PRINCIPAL = "local-owner"

#: Zusaetzliche Adressen, an denen derselbe Freigabeweg lauschen darf.
#: Kommagetrennt, ausschliesslich IP-Literale, ausschliesslich PRIVAT.
ENV_EXTRA_HOSTS = "SOLVIO_APPROVAL_EXTRA_HOSTS"


class ExtraHostRefused(ValueError):
    """Eine zusaetzliche Bindeadresse ist unzulaessig. Fail-closed, nie geraten."""


def check_extra_host(value: str) -> str:
    """Prueft eine zusaetzliche Bindeadresse — oder wirft.

    Die Regel in einem Satz: **eine zweite Bindung darf niemals ins oeffentliche
    Netz zeigen.**

    Deshalb wird ein NAME abgelehnt und nicht aufgeloest. Ein Name ist eine
    Aussage, die ein DNS-Server spaeter aendern kann; eine Bindeadresse muss im
    Moment des Lesens vollstaendig bestimmt sein. Und `0.0.0.0` ist keine
    Adresse, sondern der Verzicht auf die Frage — genau der Fehler, den dieser
    Milestone am Satellitenport vorgefunden hat.
    """
    import ipaddress

    text = (value or "").strip()
    if not text:
        raise ExtraHostRefused("leere Adresse")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ExtraHostRefused(
            f"{text!r} ist keine IP-Adresse — Namen werden nicht aufgeloest") from None
    if addr.is_unspecified:
        raise ExtraHostRefused(f"{text} bindet jede Schnittstelle")
    if addr.is_multicast:
        raise ExtraHostRefused(f"{text} ist eine Gruppenadresse")
    if addr.is_global:
        raise ExtraHostRefused(f"{text} ist oeffentlich erreichbar")
    return text


def extra_hosts_from_environment(*, primary: str) -> list[str]:
    """Liest die zusaetzlichen Bindeadressen — oder gibt keine zurueck.

    Eine unbrauchbare Angabe wird uebersprungen und protokolliert, nie geraten
    und nie stillschweigend zu `0.0.0.0` verallgemeinert. Die Hauptadresse
    faellt heraus, damit eine doppelte Angabe keinen Bindefehler erzeugt.
    """
    raw = (os.environ.get(ENV_EXTRA_HOSTS, "") or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.split(","):
        if not chunk.strip():
            continue
        try:
            host = check_extra_host(chunk)
        except ExtraHostRefused as exc:
            log.error("approvals.extra_host_refused", reason=str(exc))
            continue
        if host == primary or host in out:
            continue
        out.append(host)
    return out


class ApproverRuntime:
    """Haelt Kontrollebene, Koordinator und die LAN-Fläche fuers iPhone."""

    def __init__(self, *, host: str, port: int, state_dir: str, runtime_mode: str) -> None:
        self.host = host
        self.port = port
        self.state_dir = state_dir
        self.runtime_mode = runtime_mode
        self.control_plane: Any = None
        self.coordinator: Any = None
        self.approvals: Any = None
        self.browser_sessions: Any = None
        #: Wird vom Core vor `start()` gesetzt. Ohne ihn gibt es
        #: kein Kontrollzentrum — der Freigabeweg laeuft trotzdem.
        self.dispatcher: Any = None
        #: Der laufende CoreServer. Nur der Sprachweg liest ihn; ohne ihn
        #: wird der Sprachweg gar nicht erst angehaengt.
        self.voice_server: Any = None
        self.control_center: Any = None
        self._runner: Any = None
        self._stopping = False
        self._store: Any = None
        self.tls_fingerprint = ""
        self.core_instance_id = ""
        #: Die Adressen, an denen wirklich gelauscht wird — erst nach `start()`
        #: gefuellt. Die erste ist die LAN-Adresse und die einzige, ohne die es
        #: keinen Freigabeweg gibt.
        self.bound_hosts: list[str] = []

    async def start(self) -> None:
        """Baut den Freigabeweg auf. Wirft, wenn irgendetwas fehlt."""
        from aiohttp import web

        from solvio.capabilities.approval_gateway import CapabilityApprovals
        from solvio.security.approval import ApprovalBroker
        from solvio.security.mobile_approval import app_attest as AA
        from solvio.security.mobile_approval import bridge as B
        from solvio.security.mobile_approval import control as C
        from solvio.security.mobile_approval import gateway as G
        from solvio.security.mobile_approval import identity, pairing
        from solvio.security.mobile_approval import store as S

        self._stopping = False
        core_id = identity.load_or_create_core_instance_id(self.state_dir)
        mac_key = identity.MacSigningKey.load_or_create(self.state_dir)
        store = S.ApprovalControlStore(os.path.join(self.state_dir,
                                                    "approval_control.sqlite3"))
        self._store = store  # Own a partial DB open before the first await.
        await store.open()

        team = os.environ.get("SOLVIO_APP_ATTEST_TEAM", "WQ8CG7R53R")
        bundle = os.environ.get("SOLVIO_APP_ATTEST_BUNDLE", "de.solvio.approvals")
        allowed = AA.allowed_environments_for_mode(self.runtime_mode)
        verifier = AA.AppleAppAttestVerifier(team_id=team, bundle_id=bundle,
                                             allowed_environments=allowed)
        if getattr(verifier, "is_fake", False):
            # Kein `assert`: unter `python -O` waere die Pruefung weg — und eine
            # gefaelschte Attestierung genau dort, wo sie am meisten schadet.
            raise RuntimeError("refusing to start with a fake App Attest verifier")

        self.control_plane = C.MobileApprovalControlPlane(
            store, mac_key, core_id, attest_verifier=verifier,
            app_id=AA.app_id_for(team, bundle), allowed_environments=allowed)
        approver = B.MobileApprover()
        self.coordinator = B.MobileApprovalCoordinator(
            self.control_plane, ApprovalBroker(approver=approver), approver)
        self.approvals = CapabilityApprovals(self.coordinator,
                                             owner_principal=OWNER_PRINCIPAL)

        # Einmal in das Journal schauen, bevor bedient wird — und NUR schauen. Ein
        # neu startender Mac ist kein Grund, in der Welt etwas zu tun.
        recovery = await self.coordinator.startup_recovery_scan()
        log.info("approvals.startup_recovery", open=recovery["open"],
                 closed_no_effect=recovery["closed_no_effect"],
                 needs_attention=len(recovery["needs_attention"]),
                 retryable=len(recovery["retryable"]),
                 reconcilable=len(recovery["reconcilable"]))
        for entry in recovery["needs_attention"]:
            log.error("approvals.manual_recovery_required",
                      approval_id=entry["approval_id"], capability=entry["capability"],
                      status=entry["status"], decision=entry["decision"])

        # Waisen schliessen, BEVOR das iPhone die Liste sehen kann. Andernfalls
        # zeigte der erste Abruf nach einem Neustart genau die Anfragen, die
        # niemand mehr ausfuehren kann.
        orphans = await self.approvals.abandon_orphans()
        if orphans:
            log.info("approvals.orphans_closed", count=orphans)

        cert, key, self.tls_fingerprint = pairing.load_or_create_gateway_cert(
            self.state_dir, host=self.host)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)

        app = G.build_app(control_plane=self.control_plane, coordinator=self.coordinator)
        app.middlewares.insert(0, self._shutdown_guard())
        self._attach_browser_sessions(app)
        # Das Kontrollzentrum haengt sich an dieselbe Anwendung — gleiche TLS,
        # gleiche Geraetekennung, gleicher gepinnter Anker. Angehaengt statt
        # eingebaut: der Freigabe-Gateway bleibt Zeile fuer Zeile so, wie er
        # freigegeben wurde.
        self._attach_control_center(app)
        # Und der Sprachweg des iPhones — dieselbe Anwendung, dieselbe TLS,
        # dieselbe Geraetekennung. Auch hier angehaengt statt eingebaut.
        self._attach_voice_endpoint(app)
        # Und die WISSEN-Leserouten. Auch sie angehaengt statt eingebaut, und
        # auch sie duerfen ausfallen, ohne den Freigabeweg mitzunehmen.
        self._attach_memory_endpoint(app)
        # Und der WISSEN-Schreibweg — eine einzelne Mutation je App-Attest-
        # Beweis, DURCH den CapabilityRouter (ADR-0022). Auch er angehaengt
        # statt eingebaut, auch er darf ausfallen, ohne den Freigabeweg
        # mitzunehmen.
        self._attach_memory_mutation_endpoint(app)
        # Und der Tresor-Weg — Zugaenge lesen, Zugaenge aendern, jede Aenderung
        # ueber eine eigene App-Attest-Assertion und DURCH den Router. Auch er
        # angehaengt statt eingebaut, auch er darf ausfallen, ohne den
        # Freigabeweg mitzunehmen.
        self._attach_vault_endpoint(app)
        self._attach_payment_endpoint(app)
        self._attach_agent_endpoint(app)
        self._attach_dashboard(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port, ssl_context=context)
        await site.start()
        self.bound_hosts = [self.host]
        self.core_instance_id = core_id
        log.info("approvals.gateway_listening", host=self.host, port=self.port,
                 runtime=self.runtime_mode, app_attest_env="+".join(allowed),
                 tls=self.tls_fingerprint[:16])
        await self._bind_extra_hosts(context)

    async def _bind_extra_hosts(self, context) -> None:
        """Haengt dieselbe Anwendung an zusaetzliche PRIVATE Adressen.

        Warum ueberhaupt: der Fernzugriff (Remote Access V1) erreicht den Mac
        ueber die Overlay-Adresse des bestehenden WireGuard-Tunnels. Gemessen am
        2026-09-04: der Mac kann ein Paket mit seiner LAN-Adresse als ABSENDER
        nicht in den Tunnel geben — eine Anfrage an die LAN-Adresse kommt an,
        die Antwort verlaesst den Rechner nie. Der Vermittler schreibt das Ziel
        deshalb auf die Overlay-Adresse um, und dafuer muss hier gelauscht
        werden. Dieselbe Anwendung, dasselbe Zertifikat, dieselbe
        Geraetepruefung — nur eine zweite Bindung.

        Zwei Eigenschaften, die diese Erweiterung tragen:

        * **Nie oeffentlich.** Eine global routbare Adresse wird abgelehnt, laut
          und ohne Bindung. Der eingefrorene Kern sagt ueber sich selbst „Never
          bind the public internet / Hetzner"; diese Zeile macht daraus eine
          Pruefung statt einer Zusage.
        * **Nie toedlich.** Faellt eine zusaetzliche Bindung aus — der Tunnel
          ist unten, die Adresse existiert nicht —, laeuft der Freigabeweg auf
          der LAN-Adresse unveraendert weiter. Der Fernweg ist ein Zusatz, nie
          eine Voraussetzung.
        """
        from aiohttp import web

        for extra in extra_hosts_from_environment(primary=self.host):
            try:
                await web.TCPSite(self._runner, extra, self.port,
                                  ssl_context=context).start()
            except OSError as exc:
                # Kein Abbruch: eine fehlende Tunneladresse darf den Freigabeweg
                # nicht mitnehmen. Sie ist laut, damit sie nicht unbemerkt fehlt.
                log.error("approvals.extra_bind_failed", host=extra,
                          port=self.port, error=type(exc).__name__)
                continue
            self.bound_hosts.append(extra)
            log.info("approvals.gateway_extra_listening", host=extra,
                     port=self.port)


    def _attach_browser_sessions(self, app) -> None:
        from solvio.security.mobile_approval import browser_sessions as B
        host = f"[{self.host}]" if ":" in self.host else self.host
        configured = os.environ.get("SOLVIO_DASHBOARD_ORIGINS", "")
        origins = ({item.strip() for item in configured.split(",") if item.strip()}
                   if configured else {f"https://{host}:{self.port}"})
        try:
            from solvio.security.mobile_approval import observer_contract as O
            observer = {}
            origin = f"https://{host}:{self.port}"
            try:
                if origin in origins:
                    observer = {"observer_origin": O.local_origin(origin),
                                "observer_tls_fingerprint": O.fingerprint(self.tls_fingerprint)}
            except ValueError:
                pass  # Normal dashboard auth remains available; no observer fallback.
            service = B.BrowserSessionService(self.control_plane.store,
                core_instance_id=self.control_plane.core_instance_id, **observer)
            B.attach(app, service, origins)
            self.browser_sessions = service
        except ValueError:
            self.browser_sessions = None
            log.error("browser_sessions.invalid_configuration")

    def _attach_dashboard(self, app) -> None:
        from solvio.dashboard.endpoint import attach
        attach(app, owner_principal=OWNER_PRINCIPAL,
               voice_server_provider=lambda: self.voice_server,
               router_provider=lambda: getattr(self.dispatcher, "capabilities", None))

    def _attach_agent_endpoint(self, app) -> None:
        """Haengt die fuenf `/v1/agent/*`-Routen an — und laesst es, wenn die
        Laufzeit fehlt.

        Dieselbe Haltung wie ueberall hier: eine Sicht auf die Auftraege ist ein
        Produktzugewinn, kein Betriebsmittel. Faellt sie aus, steht der
        Freigabeweg trotzdem. Und ohne Orchestrator gibt es nichts zu zeigen —
        eine halbe Verdrahtung waere schlimmer als keine.
        """
        # Die Routen haengen IMMER — der Gateway steht frueher als der
        # Orchestrator, und eine Route, die es erst danach gaebe, gaebe es nie.
        # Ob dahinter etwas laeuft, entscheidet sich beim Zugriff (503 statt
        # 404). Live gefunden: vier Sekunden Unterschied genuegten.
        try:
            from solvio.agent_runtime.endpoint import attach as attach_agent
            attach_agent(app, provider=lambda: getattr(
                getattr(self, "dispatcher", None), "agent_runtime", None))
        except Exception as exc:  # noqa: BLE001 - ohne Runs-Sicht laeuft alles weiter
            log.error("agent_endpoint.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _attach_voice_endpoint(self, app) -> None:
        """Haengt den Sprachweg des iPhones an — und laesst es, wenn etwas fehlt.

        Dieselbe Haltung wie beim Kontrollzentrum: ein zweiter Sprachendpunkt
        ist ein Produktzugewinn, kein Betriebsmittel. Faellt er aus, steht der
        Freigabeweg trotzdem — und der Satellit im Wohnzimmer redet weiter.

        Ohne laufenden `CoreServer` gibt es gar keinen Weg: der Endpunkt braucht
        die Sitzungsfabrik, und eine halbe Verdrahtung waere schlimmer als
        keine.
        """
        server = getattr(self, "voice_server", None)
        if server is None:
            log.info("voice_endpoint.not_wired", reason="no_core_server")
            return
        try:
            from solvio.voice_endpoint import attach as attach_voice
            attach_voice(app, server)
            from solvio.realtime.acceptance_voice import attach as attach_voice_acceptance
            attach_voice_acceptance(server)
            from solvio.browser_voice_endpoint import attach as attach_browser_voice
            attach_browser_voice(app, server)
        except Exception as exc:  # noqa: BLE001 - ohne Sprachweg laeuft alles weiter
            log.error("voice_endpoint.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _attach_memory_endpoint(self, app) -> None:
        """Haengt den WISSEN-Leseweg an — und laesst es, wenn etwas fehlt.

        Nur LESEN. Jede Mutation laeuft als Faehigkeit ueber denselben
        Freigabeweg wie ein Kalendertermin; einen zweiten Schreibweg gibt es
        hier bewusst nicht.
        """
        dispatcher = getattr(self, "dispatcher", None)
        service = getattr(dispatcher, "memory", None) if dispatcher else None
        if service is None:
            log.info("memory_endpoint.not_wired", reason="no_memory_service")
            return
        try:
            from solvio.memory_endpoint import attach as attach_memory
            attach_memory(app, service=service,
                          adaptive=getattr(dispatcher, "adaptive_memory", None))
        except Exception as exc:  # noqa: BLE001 - ohne Ansicht laeuft alles weiter
            log.error("memory_endpoint.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _attach_memory_mutation_endpoint(self, app) -> None:
        """Haengt den WISSEN-Schreibweg an — und laesst es, wenn etwas fehlt.

        Eine Mutation je App-Attest-Beweis, und jede laeuft DURCH den
        `CapabilityRouter` an der Matrix von Approval Policy V2 — der Endpunkt
        beweist die Herkunft, er entscheidet nichts. Ohne laufenden
        `CoreServer` gibt es keinen Router und damit bewusst keinen Weg.
        """
        server = getattr(self, "voice_server", None)
        if server is None:
            log.info("memory_mutation_endpoint.not_wired", reason="no_core_server")
            return
        try:
            from solvio.memory_mutation_endpoint import attach as attach_mutation
            attach_mutation(app, server)
            from solvio.contacts_endpoint import attach as attach_contacts
            attach_contacts(app, server)
            from solvio.push_endpoint import attach as attach_push
            attach_push(app, server)
            from solvio.everyday_endpoint import attach as attach_everyday
            attach_everyday(app, server)
        except Exception as exc:  # noqa: BLE001 - ohne Schreibweg laeuft alles weiter
            log.error("memory_mutation_endpoint.attach_failed",
                      kind=type(exc).__name__, detail=str(exc)[:200])

    def _attach_payment_endpoint(self, app) -> None:
        """Haengt den Zahlungsweg an — und laesst es, wenn etwas fehlt.

        Dieselbe Haltung wie beim Tresor-Weg: ein eigener Endpunkt mit eigenem
        Domaenentrenner, eigenem Nonce-Topf und eigener geschlossener Liste.
        Ohne laufenden `CoreServer` gibt es keinen Router und damit bewusst
        keinen Weg; ohne angemeldete Zahlungsschicht ebenso. Ein fehlender
        Zahlungsweg ist kein Betriebsproblem — der Freigabeweg steht, und das
        Haus laeuft weiter.
        """
        server = getattr(self, "voice_server", None)
        if server is None:
            log.info("payment_endpoint.not_wired", reason="no_core_server")
            return
        capabilities = getattr(getattr(self, "dispatcher", None), "payment", None)
        if capabilities is None:
            log.info("payment_endpoint.not_wired", reason="no_payment")
            return
        try:
            from solvio.payment.endpoint import attach as attach_payment
            attach_payment(app, server, capabilities)
        except Exception as exc:  # noqa: BLE001 - ein fehlender Weg ist kein Absturz
            log.error("payment_endpoint.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _attach_vault_endpoint(self, app) -> None:
        """Haengt den Tresor-Weg an — und laesst es, wenn etwas fehlt.

        Dieselbe Haltung wie beim WISSEN-Schreibweg: ein eigener Endpunkt mit
        eigenem Domaenentrenner und eigener geschlossener Liste. Ohne laufenden
        `CoreServer` gibt es keinen Router und damit bewusst keinen Weg; ohne
        angemeldeten Tresor ebenso.
        """
        server = getattr(self, "voice_server", None)
        if server is None:
            log.info("vault_endpoint.not_wired", reason="no_core_server")
            return
        capabilities = getattr(getattr(self, "dispatcher", None), "secret_vault", None)
        if capabilities is None:
            log.info("vault_endpoint.not_wired", reason="no_vault")
            return
        try:
            from solvio.secret_vault.endpoint import attach as attach_vault
            attach_vault(app, server, capabilities)
        except Exception as exc:  # noqa: BLE001 - ohne Tresor-Weg laeuft alles weiter
            log.error("vault_endpoint.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _attach_control_center(self, app) -> None:
        """Haengt die Ansichten an — und laesst es, wenn etwas fehlt.

        Ein Kontrollzentrum ist Komfort. Faellt es aus, muss der Freigabeweg
        trotzdem stehen: er ist der Teil, an dem eine Entscheidung haengt.
        """
        dispatcher = getattr(self, "dispatcher", None)
        if dispatcher is None:
            return
        try:
            from solvio.config import load_settings
            from solvio.control_center.health import HealthBoard
            from solvio.control_center.probes import build as build_probes
            from solvio.control_center.routes import attach
            from solvio.control_center.snapshot import ControlCenter
            from solvio.doctor.doctor import Doctor
            from solvio.doctor.store import DoctorStore
            board = HealthBoard(build_probes(dispatcher, load_settings(), self))
            doctor = Doctor(dispatcher, board, store=DoctorStore())
            center = ControlCenter(dispatcher, board, doctor=doctor,
                                   approval_db=os.path.join(
                                       self.state_dir, "approval_control.sqlite3"))
            dispatcher.doctor = doctor
            # Der Arzt bekommt eine Sprachseite. Erst hier, weil er das
            # Gesundheitsbrett braucht — und das entsteht mit dem Freigabeweg.
            try:
                from solvio.capabilities.doctor import (
                    DoctorCapabilities, register as register_doctor)
                from solvio.tools.doctor_capability_tools import (
                    doctor_capability_tools)
                names = register_doctor(dispatcher.capabilities,
                                        DoctorCapabilities(doctor))
                for tool in doctor_capability_tools(dispatcher.capabilities,
                                                    dispatcher.capability_gate):
                    dispatcher.register(tool)
                log.info("tools.doctor_registered", count=len(names))
            except Exception as exc:  # noqa: BLE001 - ohne Sprachseite laeuft alles weiter
                log.info("tools.doctor_register_failed", kind=type(exc).__name__,
                         detail=str(exc)[:160])
            attach(app, center)
            self.control_center = center
        except Exception as exc:  # noqa: BLE001
            log.error("control.attach_failed", kind=type(exc).__name__,
                      detail=str(exc)[:200])

    def _shutdown_guard(self):
        from aiohttp import web
        @web.middleware
        async def guard(request, handler):
            if self._stopping:
                return web.json_response({"error": "core_stopping"}, status=503)
            return await handler(request)
        return guard

    async def stop(self) -> None:
        self._stopping = True
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def from_environment() -> ApproverRuntime | None:
    """Baut den Freigabeweg aus der Umgebung — oder gar nicht.

    Kein Halbzustand: fehlt eine der Angaben, gibt es keinen Freigabeweg, und
    schreibende Faehigkeiten bleiben bei `approval_required` stehen. Das ist die
    ehrlichere Lage als ein Gateway, das ohne bewiesene Attestierung lauscht.
    """
    host = (os.environ.get("SOLVIO_APPROVAL_HOST", "") or "").strip()
    mode = (os.environ.get("SOLVIO_RUNTIME_MODE", "") or "").strip().lower()
    state = (os.environ.get("SOLVIO_APPROVAL_STATE_DIR", "") or "").strip()
    if not (host and mode and state):
        log.info("approvals.not_configured", host=bool(host), mode=bool(mode),
                 state=bool(state))
        return None
    if not os.path.isdir(state):
        log.error("approvals.state_dir_missing")
        return None
    port = int(os.environ.get("SOLVIO_APPROVAL_PORT", "8770"))
    return ApproverRuntime(host=host, port=port, state_dir=state, runtime_mode=mode)
