"""Baut den Tool-Dispatcher aus den Settings (Schritt 17/18)."""
from __future__ import annotations

from solvio.integrations.home_assistant import HomeAssistant
from solvio.logging_setup import get_logger
from solvio.security.approval import ApprovalBroker
from solvio.tools.dispatcher import ToolDispatcher
from solvio.memory.service import MemoryService
from solvio.tools.memory_tools import (MemoryIntentGate, MemoryRememberTool,
                                        MemorySearchTool)
from solvio.capabilities.home_assistant import (HACapabilities, HAExposure, SPECS,
                                                 register as register_ha_capabilities)
from solvio.capabilities.invocation import CapabilityInvocationGate
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.calendar import (CalendarCapabilities,
                                          SPECS as CALENDAR_SPECS,
                                          register as register_calendar_capabilities)
from solvio.integrations.google_calendar import from_settings as calendar_from_settings
from solvio.capabilities.gmail import (GmailCapabilities, SPECS as GMAIL_SPECS,
                                       register as register_gmail_capabilities)
from solvio.integrations.gmail import from_settings as gmail_from_settings
from solvio.capabilities.deep import (DeepCapabilities, SPECS as DEEP_SPECS,
                                      register as register_deep_capabilities)
from solvio.capabilities.voice_supervisor import (
    SPECS as VOICE_SUPERVISOR_SPECS, VoiceSupervisorCapabilities,
    register as register_voice_supervisor_capabilities)
from solvio.capabilities.research_quick import (
    ResearchQuickCapabilities, SPECS as RESEARCH_QUICK_SPECS,
    register as register_research_quick_capabilities)
from solvio.capabilities.notes import SPECS as NOTE_SPECS
from solvio.capabilities.notes import NoteCapabilities
from solvio.capabilities.notes import register as register_note_capabilities
from solvio.tools.note_capability_tools import note_capability_tools
from solvio.tools.research_quick_capability_tools import research_quick_capability_tools
from solvio.capabilities.browser import (BrowserCapabilities, SPECS as BROWSER_SPECS,
                                         register as register_browser_capabilities)
from solvio.browser.runtime import BrowserRuntime, available as browser_available
from solvio.tools.browser_capability_tools import browser_capability_tools
from solvio.capabilities.portal import (PortalCapabilities, SPECS as PORTAL_SPECS,
                                       register as register_portal_capabilities)
from solvio.portal.client import PortalClient
from solvio.portal.vault import PortalVault
from solvio.tools.portal_capability_tools import portal_capability_tools
from solvio.tools.proactive_capability_tools import proactive_capability_tools
from solvio.tools.calendar_capability_tools import calendar_capability_tools
from solvio.tools.deep_capability_tools import deep_capability_tools
from solvio.tools.gmail_capability_tools import gmail_capability_tools
from solvio.capabilities.documents import (
    DocumentCapabilities, SPECS as DOCUMENT_SPECS,
    register as register_document_capabilities)
from solvio.tools.document_capability_tools import document_capability_tools
from solvio.capabilities.communication import (
    CommunicationCapabilities, SPECS as COMMUNICATION_SPECS,
    register as register_communication_capabilities)
from solvio.communication.bindings import BindingStore
from solvio.capabilities.telephony import (
    TelephonyCapabilities, SPECS as TELEPHONY_SPECS,
    register as register_telephony_capabilities)
from solvio.telephony.provider import ElevenLabsTelephonyProvider
from solvio.tools.telephony_capability_tools import telephony_capability_tools
from solvio.tools.communication_capability_tools import communication_capability_tools
from solvio.tools.ha_capability_tools import ha_capability_tools

log = get_logger("tools")

def _knowledge_refresher(memory_service=None, *, vault=None):
    """Ein Haken, der das Wissensbuendel nachzieht — ohne Ruecklauf.

    Er laeuft als Hintergrundaufgabe: eine Freigabehandlung soll nicht auf
    einen Compiler warten, und ein fehlgeschlagener Compilerlauf soll keine
    bereits vollzogene Gedaechtnisaenderung scheitern lassen. Pro Dienst laeuft
    hoechstens eine Kompilierung; Aenderungen waehrenddessen werden gesammelt
    und danach erneut gelesen. So kann kein aelterer Snapshot spaeter gewinnen.
    """
    task = None
    dirty = False

    async def drain() -> None:
        nonlocal dirty
        from solvio.knowledge.service import compile_knowledge
        while dirty:
            dirty = False
            await _compile_quietly(compile_knowledge, memory_service=memory_service,
                                   vault=vault)

    def refresh():
        nonlocal task, dirty
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        dirty = True
        if task is None or task.done():
            task = loop.create_task(drain())
        return task
    return refresh


async def _compile_quietly(compile_knowledge, *, memory_service=None, vault=None) -> None:
    try:
        if memory_service is None:
            if vault is None:
                await compile_knowledge()
            else:
                await compile_knowledge(vault)
        else:
            memory = getattr(getattr(memory_service, "semantic", None), "memory", None)
            if memory is not None:
                await compile_knowledge(vault, memory=memory)
            else:
                await compile_knowledge(vault, base_dir=memory_service.base_dir)
    except Exception as exc:  # noqa: BLE001 - eine Ansicht darf ausfallen
        log.info("knowledge.refresh_failed", kind=type(exc).__name__)


def build_dispatcher(settings) -> ToolDispatcher:
    d = ToolDispatcher()
    # Fail-closed: ohne vertrauenswuerdigen Approver kann keine MODIFY freigegeben
    # werden. Das Sprachmodell erhaelt nie ein Freigabe-Token.
    d.approvals = ApprovalBroker()

    # Der kanonische Weg fuer jede neue Faehigkeit. Router und Aufrufkontext gibt es
    # unabhaengig davon, ob HA konfiguriert ist — der Core setzt den Kontext pro Turn,
    # ohne wissen zu muessen, was gerade registriert ist.
    d.capability_gate = CapabilityInvocationGate()
    # Der Freigabeweg wird spaeter vom Core angehaengt (er braucht einen Eventloop
    # und eine LAN-Fläche). Bis dahin bleibt der fail-closed Broker-Pfad: eine
    # schreibende Faehigkeit endet dann bei `approval_required` und tut nichts.
    d.approver_runtime = None
    # Der tiefe Executor haengt der Core spaeter an: er braucht einen Eventloop,
    # ein Gefaengnis und einen laufenden Fremdprozess. Bis dahin gibt es die
    # Faehigkeit schlicht nicht — kein Halbzustand, keine Attrappe.
    d.deep_runtime = None
    # `shadow` nur, wenn es ausdruecklich dasteht — jeder andere Wert, auch ein
    # Tippfehler, laesst die Matrix entscheiden.
    policy_mode = ("shadow"
                   if str(getattr(settings, "approval_policy_mode", "")).strip().lower()
                   == "shadow" else "enforce")
    d.capabilities = CapabilityRouter(approvals=d.approvals, policy_mode=policy_mode)

    # Der Modus des kognitiven Routers. Hier nur GELESEN und abgelegt — das
    # Anhaengen passiert spaet, nach den drei Laufzeiten, deren Werkzeuge er
    # umlegt. Ein unbekannter Wert bedeutet `off`: eine vertippte
    # Konfiguration darf keine stille Einschaltung sein.
    from solvio.cognition import normalise_mode
    d.cognitive_router_mode = normalise_mode(
        getattr(settings, "cognitive_router_mode", "off"))
    log.info("tools.cognitive_router", mode=d.cognitive_router_mode)
    log.info("tools.approval_policy", mode=policy_mode)

    # Die Kurzrecherche. Kein Hermes, keine Agentenlaufzeit, kein Fremdprozess
    # — sie braucht nur den Provider Broker, und der haengt SPAET am Server,
    # nicht am Dispatcher. Registriert wird deshalb sofort (`d` selbst wird
    # gehalten, `d.provider_broker` wird beim AUFRUF gelesen, nie hier
    # gemerkt) — derselbe Weg wie beim kognitiven Router.
    d.research_quick = ResearchQuickCapabilities(d)
    register_research_quick_capabilities(d.capabilities, d.research_quick)
    for tool in research_quick_capability_tools(d.capabilities, d.capability_gate):
        d.register(tool)
    log.info("tools.research_quick_registered", count=len(RESEARCH_QUICK_SPECS),
             path="capability_contract")

    # **Der starke Antwortweg -- als Faehigkeit, NICHT als Sprachwerkzeug.**
    #
    # Er wird bewusst nur am Faehigkeitsrouter angemeldet und bekommt keinen
    # Adapter in `openai_tools()`. Damit sieht ihn das produktive Sprachmodell
    # NICHT, und die Autoritaetsflaeche des laufenden SOLVIO waechst um genau
    # nichts. Erreichbar ist er ueber `run_capability` am Kontrollsocket --
    # also fuer einen lokalen Prozess des Besitzers, ueber dasselbe Tor,
    # dieselbe Freigabematrix und denselben Broker wie jede andere Faehigkeit.
    #
    # Wer ihn spaeter dem Gespraech geben will, fuegt einen Adapter hinzu und
    # trifft damit eine EIGENE, sichtbare Entscheidung -- statt sie hier
    # nebenbei mitzunehmen.
    d.voice_supervisor = VoiceSupervisorCapabilities(d)
    register_voice_supervisor_capabilities(d.capabilities, d.voice_supervisor)
    log.info("tools.voice_supervisor_registered",
             count=len(VOICE_SUPERVISOR_SPECS), path="capability_contract",
             exposed_to_llm=False)

    # Die Notizfaehigkeit. Sie braucht keinen Anbieter und keine Anmeldung —
    # nur die autorisierte Ablage, die sie sich selbst ableitet. Deshalb
    # unbedingt registriert, wie die Kurzrecherche.
    #
    # **Mit Sprachwerkzeug, und das war eine Korrektur.** Die erste Fassung
    # liess den Adapter weg, mit der Begruendung „ein internes Werkzeug, kein
    # Nutzerbefehl". Die Messung am echten Spracheinstieg widerlegte das: der
    # kognitive Router ordnet „Schreib mir eine Notiz …" mit Zuversicht 0.98
    # als `kein_auftrag` ein — also „SOLVIO kann das SELBST, hier und jetzt".
    # Die Route ist richtig; falsch war, dass es im Gespraech dann keinen
    # Ausfuehrungsweg gab. Das Modell haette „ist notiert" sagen koennen, ohne
    # dass irgendetwas geschrieben wurde.
    #
    # Ein Werkzeug ist kein Nutzerbefehl: der Nutzer sagt „schreib mir eine
    # Notiz", nicht `note_write`. Und geschwaecht wird nichts — die Matrix
    # verlangt fuer `note_write` aus JEDER Herkunft Face ID.
    register_note_capabilities(d.capabilities, NoteCapabilities())
    for tool in note_capability_tools(d.capabilities, d.capability_gate):
        d.register(tool)
    log.info("tools.notes_registered", count=len(NOTE_SPECS),
             path="capability_contract")

    # DER GEHEIMNISTRESOR — VOR den Anbietern, und das ist die ganze Pointe.
    #
    # Jeder Anbieter unten fragt „liegt mein Zugang im Tresor?" und benutzt nur
    # dann den Rueckfall aus der Konfiguration, wenn die Antwort nein ist. Waere
    # der Tresor spaeter dran, waere die Antwort immer nein, und „migriert"
    # bliebe eine Absicht statt eines Zustands.
    #
    # Registriert werden die Verwaltungs-Faehigkeiten am Router und GENAU EIN
    # lesendes Werkzeug fuers Modell. Die Verwaltungshandlungen haben bewusst
    # kein Werkzeug: erreichbar sind sie nur ueber den attestierten Tresor-Weg
    # des iPhones. Ein Modell kann sie nicht aufrufen, weil es sie nicht nennen
    # kann. Und ein `get_secret` gibt es nirgends.
    try:
        from solvio.capabilities.secret_vault import (
            SecretVaultCapabilities, register as register_vault)
        from solvio.secret_vault.broker import SecretBroker
        from solvio.tools.secret_vault_tools import secret_vault_tools
        d.secret_vault = SecretVaultCapabilities()
        d.secret_broker = SecretBroker(d.secret_vault.store)
        names = register_vault(d.capabilities, d.secret_vault)
        for tool in secret_vault_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.secret_vault_registered", count=len(names),
                 secrets=len(d.secret_broker.catalogue()))
    except Exception as exc:  # noqa: BLE001 - ohne Tresor laeuft alles weiter,
        # nur eben mit den Zugaengen aus der Konfiguration.
        d.secret_vault = None
        d.secret_broker = None
        log.error("tools.secret_vault_unavailable", kind=type(exc).__name__)
    broker = getattr(d, "secret_broker", None)

    # DIE ZAHLUNG — NACH dem Tresor, und das ist dieselbe Pointe.
    #
    # Der Zahlungs-Executor leiht seinen Anbieterzugang ueber den Makler; ohne
    # angemeldeten Tresor gibt es keinen, und dann soll die Zahlungsschicht
    # ehrlich fehlen statt halb dazustehen.
    #
    # Registriert werden vierzehn Faehigkeiten am Router und GENAU VIER
    # Werkzeuge fuers Modell — und keines davon bewegt Geld. `purchase_place`,
    # Stornierung, Erstattung und die ganze Verwaltung haben bewusst kein
    # Werkzeug: erreichbar sind sie nur ueber den attestierten Zahlungsweg des
    # iPhones. Ein Modell kann sie nicht aufrufen, weil es sie nicht nennen kann.
    try:
        from solvio.capabilities.payment import (PaymentCapabilitiesFull,
                                                 register as register_payment)
        from solvio.payment.executor import PaymentExecutor
        from solvio.payment.store import PaymentStore
        from solvio.tools.payment_capability_tools import payment_capability_tools
        _payment_store = PaymentStore()
        d.payment = PaymentCapabilitiesFull(
            _payment_store,
            executor=PaymentExecutor(_payment_store, broker=broker))
        names = register_payment(d.capabilities, d.payment)
        for tool in payment_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.payment_registered", count=len(names),
                 methods=len(_payment_store.instruments()))
    except Exception as exc:  # noqa: BLE001 - ohne Zahlungsweg laeuft alles
        # weiter. Ein fehlender Anbieter darf den Core nie am Starten hindern.
        d.payment = None
        log.error("tools.payment_unavailable", kind=type(exc).__name__)

    # „Eingerichtet" heisst seit dem Tresor: Adresse da UND ein Zugang
    # IRGENDWO — in der Konfiguration oder im Tresor.
    #
    # `settings.has_home_assistant` verlangt beides in `.env`. Nach der
    # Wanderung war der Token dort weg, und der Core meldete daraufhin gar
    # keine Haus-Faehigkeiten mehr an: jeder Aufruf fiel als
    # `unknown_capability` durch, und das sah aus wie eine Policy-Absage.
    # Live gefunden, direkt nach dem Entfernen des Klartexts.
    d.task_action_clients = {}
    ha_url = (getattr(settings, "home_assistant_url", "") or "").strip()
    ha_from_vault = (broker is not None
                     and broker.exists(HomeAssistant.CREDENTIAL_REF))
    if ha_url and (ha_from_vault or getattr(settings, "has_home_assistant", False)):
        ha = HomeAssistant(
            ha_url,
            "" if ha_from_vault else settings.home_assistant_token,
            broker=broker if ha_from_vault else None)
        log.info("tools.ha_credential", source="vault" if ha_from_vault else "config")
        exposure = HAExposure(ha)
        # Die ausdrueckliche Sicherheitsliste des Besitzers. Sie kommt aus der
        # Konfiguration und nie aus einer Modellausgabe: ein Geraet zur
        # Haustechnik zu erklaeren, ist eine Entscheidung des Menschen.
        security_entities = frozenset(
            part.strip() for part in
            str(getattr(settings, "home_assistant_security_entities", "")).split(",")
            if part.strip())
        register_ha_capabilities(d.capabilities,
                                 HACapabilities(exposure, security_entities))
        d.task_action_clients.update(ha=ha, exposure=exposure, security_entities=security_entities)
        if security_entities:
            log.info("tools.ha_security_entities", count=len(security_entities))
        for tool in ha_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        # Nicht mehr hart 5: die Zahl kommt aus dem, was wirklich registriert wurde.
        log.info("tools.ha_registered", count=len(SPECS), path="capability_contract")
    else:
        log.info("tools.ha_not_configured")

    calendar_provider = calendar_from_settings(settings, broker=broker)
    d.task_action_clients["calendar"] = calendar_provider
    if calendar_provider is not None:
        register_calendar_capabilities(d.capabilities,
                                       CalendarCapabilities(calendar_provider))
        for tool in calendar_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.calendar_registered", count=len(CALENDAR_SPECS),
                 path="capability_contract")
    else:
        # Kein Halbzustand: ohne hinterlegte Anmeldung existiert die Faehigkeit
        # gar nicht, statt dem Modell ein Werkzeug zu zeigen, das immer scheitert.
        log.info("tools.calendar_not_configured")

    gmail_provider = gmail_from_settings(settings, broker=broker)
    d.task_action_clients["gmail"] = gmail_provider
    if gmail_provider is not None:
        gmail = GmailCapabilities(gmail_provider)
        register_gmail_capabilities(d.capabilities, gmail)
        for tool in gmail_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.gmail_registered", count=len(GMAIL_SPECS),
                 path="capability_contract")
        documents = DocumentCapabilities(gmail_provider, d)
        register_document_capabilities(d.capabilities, documents)
        for tool in document_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.documents_registered", count=len(DOCUMENT_SPECS),
                 path="capability_contract")
        d.communication = CommunicationCapabilities(gmail, BindingStore())
        register_communication_capabilities(d.capabilities, d.communication)
        for tool in communication_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.communication_registered", count=len(COMMUNICATION_SPECS),
                 path="capability_contract")
    else:
        log.info("tools.gmail_not_configured")

    # Telefonie (Telephony Capability V1). Ohne vollstaendige Einrichtung wird
    # die Faehigkeit GAR NICHT registriert — dann gibt es kein Werkzeugschema,
    # das Modell sieht sie nicht, und es kann sie auch nicht erraten. Das ist
    # die aeusserste der drei Schranken; die beiden anderen sind die Freigabe
    # (`telephony_call` steht in VERY_CRITICAL_BY_BIRTH) und die bestaetigte
    # Kontaktbindung.
    d.telephony = None
    if settings.has_telephony:
        # Gekapselt wie der Tresor- und der Zahlungsblock daneben, und aus
        # demselben Grund: `TelephonyCapabilities(...)` oeffnet im Konstruktor
        # den Ledger unter `~/.solvio/telephony.sqlite3`. Ist diese Datei
        # beschaedigt oder gesperrt, wirft `CallLedger`, `build_dispatcher`
        # wirft weiter — und der Aufruf beim Hochfahren steht ausserhalb jedes
        # `try`. Der Core startet dann GAR NICHT: kein Sprachweg, kein Satellit,
        # kein iPhone. Ein kaputtes Anrufbuch darf den ganzen Assistenten nicht
        # mitnehmen; ohne Telefonie laeuft alles andere weiter.
        try:
            telephony = TelephonyCapabilities(
                ElevenLabsTelephonyProvider(
                    agent_id=settings.telephony_agent_id,
                    phone_number_id=settings.telephony_phone_number_id),
                BindingStore(),
                max_duration_secs=settings.telephony_max_duration_secs)
            d.telephony = telephony
            register_telephony_capabilities(d.capabilities, telephony)
            for tool in telephony_capability_tools(d.capabilities, d.capability_gate):
                d.register(tool)
            log.info("tools.telephony_registered", count=len(TELEPHONY_SPECS),
                     path="capability_contract")
        except Exception as exc:  # noqa: BLE001
            d.telephony = None
            log.error("tools.telephony_unavailable", kind=type(exc).__name__,
                      detail=str(exc)[:200])
    else:
        log.info("tools.telephony_not_configured")

    # Der Browser startet erst beim ersten Bedarf. Registriert wird er trotzdem
    # sofort: ein Chrome, der beim Hochfahren des Core mitlaeuft, waere ein
    # Prozess, der die meiste Zeit nur Speicher haelt.
    d.browser = None
    if browser_available():
        d.browser = BrowserRuntime()
        register_browser_capabilities(d.capabilities, BrowserCapabilities(d.browser))
        for tool in browser_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.browser_registered", count=len(BROWSER_SPECS),
                 path="capability_contract")
    else:
        log.info("tools.browser_not_available")

    # Angemeldete Portale gibt es nur, wenn der Arbeiter unter einer eigenen
    # Unix-Kennung laeuft. Fehlt er, fehlt die Faehigkeit — nicht ersatzweise ein
    # angemeldeter Browser im Core, denn das waere genau die Trennung, die dieser
    # Weg herstellen soll.
    d.portal = None
    portal_client = PortalClient()
    if portal_client.available():
        d.portal = portal_client
        portal_capabilities = PortalCapabilities(portal_client, PortalVault())
        d.task_action_clients["portals"] = portal_capabilities
        register_portal_capabilities(
            d.capabilities, portal_capabilities)
        for tool in portal_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.portal_registered", count=len(PORTAL_SPECS),
                 path="capability_contract")
    else:
        log.info("tools.portal_worker_absent")

    # M2: Langzeitgedaechtnis. Faellt es aus, laeuft die Sprachschicht ohne — die
    # Stimme darf nicht daran haengen, ob ein Einbettungsmodell erreichbar ist.
    d.memory = None
    d.memory_gate = MemoryIntentGate()
    try:
        service = MemoryService().open()
        d.memory = service
        d.register(MemoryRememberTool(service, d.memory_gate))
        d.register(MemorySearchTool(service))
        provider_id = service.provider.profile.provider_id
        if provider_id == "qwen-local":
            log.info("tools.memory_registered", base_dir=service.base_dir,
                     provider=provider_id, degraded=False)
        else:
            # Nicht als "gesund" durchwinken: der Rueckfall ist rein lexikalisch.
            log.warning("tools.memory_degraded", base_dir=service.base_dir,
                        provider=provider_id, degraded=True,
                        reason="local embedding model unavailable")
    except Exception as exc:  # noqa: BLE001
        log.error("tools.memory_unavailable", kind=type(exc).__name__)

    # Adaptive Memory. Haengt vollstaendig am kanonischen Dienst: ohne ihn gibt
    # es kein Lernen, und das ist kein Halbzustand, sondern die ehrliche
    # Abwesenheit einer Funktion. Faellt der Extraktor aus, laeuft alles
    # uebrige unveraendert weiter — Lernen ist nie im Antwortpfad.
    d.adaptive_memory = None
    if d.memory is not None:
        try:
            from solvio.capabilities.memory import (
                MemoryCapabilities, SPECS as MEMORY_SPECS,
                register as register_memory_capabilities)
            from solvio.memory.adaptive.candidates import CandidateStore
            from solvio.memory.adaptive.extractor import from_settings as extractor_from
            from solvio.memory.adaptive.pipeline import AdaptiveMemory

            enabled = bool(getattr(settings, "adaptive_memory_enabled", True))
            # Der Abo-Antwortweg ist unabhaengig vom spaeteren Brokerstart.
            # Ohne expliziten Kostenkontext dieser Beobachtung fuehrt der
            # Extraktor trotzdem nichts aus. Es gibt keinen API-Rueckfall.
            extractor = extractor_from(settings)
            adaptive = AdaptiveMemory(
                d.memory, CandidateStore(d.memory.base_dir),
                extractor=extractor, enabled=enabled)
            d.adaptive_memory = adaptive
            # Der eine Ort, an dem Gedaechtnis und Wissen sich kennen duerfen.
            # Der Zusammenbau ist weder Sicherheits- noch Faehigkeitspfad.
            d.memory.on_change = _knowledge_refresher(d.memory)
            # Die fuenf mutierenden Wege — jeder ueber den Freigabeweg mit
            # Face ID, keiner am Gateway vorbei.
            register_memory_capabilities(
                d.capabilities, MemoryCapabilities(d.memory, adaptive))
            # Und die Bruecke zum Sprachweg. Ohne sie haetten die fuenf
            # Faehigkeiten keinen Aufrufer, bis der WISSEN-Bildschirm existiert —
            # und "vergiss das" waere eine Bitte ohne Wirkung.
            from solvio.tools.memory_capability_tools import memory_capability_tools
            for tool in memory_capability_tools(d.capabilities, d.capability_gate):
                d.register(tool)
            log.info("tools.adaptive_memory_registered",
                     capabilities=len(MEMORY_SPECS),
                     extractor=getattr(extractor, "name", "unknown"),
                     enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            d.adaptive_memory = None
            log.error("tools.adaptive_memory_unavailable",
                      kind=type(exc).__name__, detail=str(exc)[:200])

    # Der Hintergrund. Speicher und Faehigkeiten entstehen hier; die Schleife
    # startet erst der Core, weil sie einen laufenden Eventloop braucht.
    try:
        from solvio.capabilities.proactive import (
            ProactiveCapabilities, register as register_proactive,
        )
        from solvio.proactive.store import ProactiveStore
        d.proactive_store = ProactiveStore()
        # Der Pruefer gebundener Erlaubnisse. Er haengt am selben Speicher wie
        # die Automatisierungen — Autoritaet in einer eigenen Zeile, aber an
        # der bestehenden Naht statt in einer zweiten Datenbank.
        from solvio.capabilities.preauth import AutomationPreauthorizations
        d.preauthorizations = AutomationPreauthorizations(d.proactive_store)
        d.capabilities._preauth = d.preauthorizations
        d.proactive = ProactiveCapabilities(d.proactive_store,
                                            gate=d.capability_gate,
                                            preauth=d.preauthorizations,
                                            router=d.capabilities)
        names = register_proactive(d.capabilities, d.proactive)
        for tool in proactive_capability_tools(d.capabilities, d.capability_gate):
            d.register(tool)
        log.info("tools.proactive_registered", count=len(names),
                 path="capability_contract")
    except Exception as exc:  # noqa: BLE001
        d.proactive_store = None
        d.proactive = None
        log.error("tools.proactive_unavailable", kind=type(exc).__name__)

    # Der Untersucher. Zuletzt, weil er beide Inventare aus dem fertigen
    # Dispatcher liest — waere er frueher da, kennte er die Haelfte nicht.
    # Faellt er aus, verhaelt sich SOLVIO exakt wie vorher: eine Blockade bleibt
    # eine Blockade, nur ohne die Untersuchung dahinter.
    try:
        from solvio.resolver.wiring import build_resolver
        d.gap_resolver = build_resolver(d, settings)
        log.info("tools.resolver_registered",
                 capabilities=len(d.capabilities.names()),
                 runtimes=len(d.gap_resolver.runtimes.keys()),
                 research=d.gap_resolver.researcher is not None)
    except Exception as exc:  # noqa: BLE001
        d.gap_resolver = None
        log.error("tools.resolver_unavailable", kind=type(exc).__name__)
    return d


def attach_agent_runtime(dispatcher, orchestrator) -> list[str]:
    """Haengt die Agentenlaufzeit an den laufenden Core.

    Spaet und nicht in `build_dispatcher`, nach dem Muster von
    `attach_deep_runtime`: die Laufzeit braucht einen Eventloop, ein Ledger und
    den Freigabeweg. Schlaegt das Anhaengen fehl, laeuft SOLVIO vollstaendig
    weiter — nur ohne Agentenauftraege. Genau das ist auch die Rollback-Zusage:
    ohne diesen Aufruf gibt es die fuenf Faehigkeiten nicht.
    """
    from solvio.capabilities.agent import (AgentCapabilities, SPECS as AGENT_SPECS,
                                           register as register_agent_capabilities)
    from solvio.tools.agent_capability_tools import agent_capability_tools

    dispatcher.agent_runtime = orchestrator
    from solvio.tools.personal_task import PersonalTaskTool
    if dispatcher.tool("personal_task") is None:
        dispatcher.register(PersonalTaskTool(dispatcher))
    from solvio.tools.task_continue import TaskContinueTool
    if dispatcher.tool('task_continue') is None:
        dispatcher.register(TaskContinueTool(dispatcher))
    from solvio.tools.task_answer import TaskAnswerTool
    if dispatcher.tool('task_answer') is None:
        dispatcher.register(TaskAnswerTool(dispatcher))
    # Same configured integrations; no second credentials or service registry.
    # Reattachment replaces the mediator's ledger along with the runtime.
    from solvio.capabilities.task_action import SPEC as ACTION_SPEC, TaskServiceAction
    clients = getattr(dispatcher, "task_action_clients", {})
    service = TaskServiceAction(orchestrator.ledger, control_plane=orchestrator.control_plane, **clients)
    if dispatcher.capabilities.spec(ACTION_SPEC.name) is None:
        dispatcher.capabilities.register(ACTION_SPEC, service)
    else:
        dispatcher.capabilities._handlers[ACTION_SPEC.name] = service
    orchestrator.action_service = service
    note_tool = dispatcher.tool("note_write")
    if note_tool is not None:
        note_tool.ledger = getattr(orchestrator, "ledger", None)
    for name in ("gmail_send_draft", "communication_send", "communication_confirm_binding",
                 "mail_forward", "mail_send", "mail_reply", "mail_followup", "background_create"):
        send_tool = dispatcher.tool(name)
        if send_tool is not None:
            send_tool.ledger = getattr(orchestrator, "ledger", None)
    existing = getattr(dispatcher, "agent_capabilities", None)
    if existing is not None:
        # Ein zweites Mal: der Orchestrator wurde neu gebaut. Die Faehigkeiten
        # stehen schon im Router; was sich aendern MUSS, ist das Objekt
        # dahinter — sonst zeigt die Sicht auf den neuen und jeder Auftrag auf
        # den toten. Dieselbe Lehre wie beim tiefen Executor.
        existing.orchestrator = orchestrator
        log.info("tools.agent_runtime_reattached", count=len(AGENT_SPECS))
        return sorted(AGENT_SPECS)

    capabilities = AgentCapabilities(orchestrator)
    dispatcher.agent_capabilities = capabilities
    register_agent_capabilities(dispatcher.capabilities, capabilities)
    # N8/C4: the READ_ONLY list of a task's own published result files, exposed
    # to native workers as a DynamicTool (same grant/cost seams as portal_list).
    from solvio.agent_runtime.result_files import LIST_CAPABILITY, register as register_result_files
    if dispatcher.capabilities.spec(LIST_CAPABILITY) is None:
        register_result_files(dispatcher.capabilities, orchestrator.ledger)
    from solvio.agent_runtime.task_overview import NAME as OVERVIEW, register as register_overview
    if dispatcher.capabilities.spec(OVERVIEW) is None:
        register_overview(dispatcher.capabilities, orchestrator.ledger)
    for tool in agent_capability_tools(dispatcher.capabilities,
                                       dispatcher.capability_gate,
                                       getattr(orchestrator, "ledger", None)):
        dispatcher.register(tool)
    # Nachzug wie beim Resolver: kommt der tiefe Executor erst spaeter (oder
    # nach einem `doctor hermes_restart`), bekaeme die Laufzeit sonst nie einen
    # Rechercheweg — und „keine Recherche" saehe aus wie „nichts gefunden".
    if getattr(orchestrator, "researcher", None) is None:
        deep = getattr(dispatcher, "deep_capabilities", None)
        if deep is not None:
            orchestrator.researcher = deep
            log.info("tools.agent_runtime_researcher_attached")
    log.info("tools.agent_runtime_registered", count=len(AGENT_SPECS),
             path="capability_contract")
    return sorted(AGENT_SPECS)


def attach_deep_runtime(dispatcher, runtime) -> list[str]:
    """Haengt den tiefen Executor an den laufenden Core.

    Spaet und nicht in `build_dispatcher`, weil ein Fremdprozess im Gefaengnis
    erst starten und sich seine Werkzeugflaeche abnehmen lassen muss. Schlaegt das
    fehl, laeuft SOLVIO vollstaendig weiter — nur ohne Recherche.
    """
    dispatcher.deep_runtime = runtime
    existing = getattr(dispatcher, "deep_capabilities", None)
    if existing is not None:
        # Ein zweites Mal — der Arzt hat Hermes neu gestartet. Die Faehigkeiten
        # stehen bereits im Router, und ihn erneut zu bestuecken ist ein Fehler
        # (er wehrt sich zu Recht). Was sich aendern MUSS, ist der Prozess
        # dahinter: sonst zeigt die Messung auf den neuen und jede Recherche auf
        # den toten. Genau so entsteht eine zweite Wahrheit.
        existing.runtime = runtime
        log.info("tools.deep_reattached", count=len(DEEP_SPECS))
        return sorted(DEEP_SPECS)

    capabilities = DeepCapabilities(runtime)
    dispatcher.deep_capabilities = capabilities
    register_deep_capabilities(dispatcher.capabilities, capabilities)
    for tool in deep_capability_tools(dispatcher.capabilities, dispatcher.capability_gate):
        dispatcher.register(tool)
    log.info("tools.deep_registered", count=len(DEEP_SPECS), path="capability_contract")
    resolver = getattr(dispatcher, "gap_resolver", None)
    if resolver is not None and resolver.researcher is None:
        # Der Resolver entsteht, bevor Hermes laeuft. Ohne diese Nachreichung
        # bliebe die Recherche fuer die ganze Laufzeit aus — und niemand haette
        # es gemerkt, weil „keine Recherche" wie „nichts gefunden" aussieht.
        from solvio.resolver.wiring import build_researcher
        resolver.researcher = build_researcher(dispatcher)
        # Der Kundschafter des Fachteams laeuft ueber denselben Weg — sonst
        # bliebe die Rolle stumm, obwohl Hermes inzwischen da ist.
        team = getattr(resolver, "team", None)
        if team is not None and team.researcher is None:
            team.researcher = resolver.researcher
        log.info("resolver.research_attached",
                 available=resolver.researcher is not None)
    return sorted(DEEP_SPECS)


def attach_bot_team(dispatcher, team) -> list[str]:
    """Haengt das Botteam an den laufenden Core.

    Spaet und nicht in `build_dispatcher`, aus demselben Grund wie beim tiefen
    Executor: die Profile im Gefaengnis muessen erst angelegt und mit SOLVIOs
    Haltung beschrieben werden, und das ist Dateiarbeit plus ein Kindprozess.
    Schlaegt es fehl, laeuft SOLVIO vollstaendig weiter — nur ohne Fachauskunft.

    Der Diagnostiker bekommt seine Befunde ueber eine **Funktion**, die den
    Arzt erst beim Aufruf sucht. Wuerde hier ein Gesundheitsbrett festgehalten,
    haette der Bot ein Objekt in der Hand, das messen kann — und dann waere die
    Messung seine.
    """
    from solvio.capabilities.bots import (BotCapabilities, SPECS as BOT_SPECS,
                                          register as register_bot_capabilities)
    from solvio.tools.bot_capability_tools import bot_capability_tools

    existing = getattr(dispatcher, "bot_capabilities", None)
    if existing is not None:
        existing.team = team
        log.info("tools.bots_reattached", count=len(BOT_SPECS))
        return sorted(BOT_SPECS)

    async def evidence():
        return await _health_evidence(dispatcher)

    capabilities = BotCapabilities(team, evidence=evidence)
    dispatcher.bot_team = team
    dispatcher.bot_capabilities = capabilities
    register_bot_capabilities(dispatcher.capabilities, capabilities)
    for tool in bot_capability_tools(dispatcher.capabilities, dispatcher.capability_gate):
        dispatcher.register(tool)
    log.info("tools.bots_registered", count=len(BOT_SPECS), path="capability_contract")
    return sorted(BOT_SPECS)


def attach_cognition(dispatcher, *, mode: str = "off", router=None):
    """Haengt den kognitiven Router an den laufenden Core.

    Spaet und nicht in `build_dispatcher`, weil die vier Werkzeuge, um die es
    geht, selbst erst spaet entstehen: `deep_research` mit dem tiefen Executor,
    `bot_consult` mit dem Botteam, die zwei `agent_task_*` mit der
    Agentenlaufzeit. Wer frueher umlegt, legt nichts um.

    **Ohne diesen Aufruf existiert `solvio_task` nicht**, die vier Werkzeuge
    stehen unveraendert vor dem Modell, und die Anweisung ist die von gestern.
    Das ist die Rollback-Zusage, und sie ist ein Konfigurationswert weit.

    `shadow` misst und wirkt nicht: die Oberflaeche bleibt unveraendert, das
    Modell waehlt wie bisher, und NACH dem echten Ergebnis schreibt eine
    Einschaetzung auf, was der Router gewaehlt haette — je finalisiertem Turn,
    nicht je Werkzeug. `active` legt die Belichtung um.

    Idempotent: ein zweiter Aufruf richtet nichts an. Ein `doctor
    hermes_restart` haengt den tiefen Executor neu an, ohne die Werkzeuge neu
    zu bauen — das umgelegte Kennzeichen ueberlebt das, und ein doppelt
    gelegter Schatten waere ein doppelter Modellaufruf je Turn.
    """
    from solvio.cognition import normalise_mode
    from solvio.cognition.router import HIDDEN_IN_ACTIVE, CognitiveRouter
    from solvio.tools.cognition_tools import cognition_tools

    chosen = normalise_mode(mode)
    if chosen == "off":
        log.info("tools.cognition_off")
        return []

    existing = getattr(dispatcher, "cognition", None)
    if existing is not None:
        existing.mode = chosen
        log.info("tools.cognition_reattached", mode=chosen)
        return sorted(HIDDEN_IN_ACTIVE)

    engine = router if router is not None else CognitiveRouter(dispatcher,
                                                               mode=chosen)
    dispatcher.cognition = engine

    if chosen == "shadow":
        # Der Schatten haengt KEIN Werkzeug um. Die Sprachschicht bietet jeden
        # finalisierten Turn zur Messung an (`Session._offer_shadow`) — auch
        # die, in denen kein Werkzeug lief. Genau die waren mit dem
        # Vier-Werkzeug-Wrapper unsichtbar, und genau in ihnen steckt
        # „verpasste Arbeit".
        log.info("tools.cognition_shadow", observer="turn")
        return []

    for tool in cognition_tools(engine, dispatcher.capability_gate):
        dispatcher.register(tool)
    hidden = []
    for name in HIDDEN_IN_ACTIVE:
        tool = dispatcher.tool(name)
        if tool is None:
            # Der Weg gibt es heute nicht — dann ist auch nichts zu verbergen.
            # Der Router meldet ihn spaeter als `route_unavailable`, was er ist.
            continue
        # JE INSTANZ, nicht je Klasse: `expose_to_llm` ist ein
        # Klassenattribut, und die DREI Namen gehoeren ZWEI Klassen an —
        # `bot_consult` einer, `agent_task_research` und `agent_task_build`
        # teilen sich ihre mit `agent_run_cancel`, `agent_run_status` und
        # `agent_run_resume`. Wer die Klasse umlegt, verbirgt genau die
        # Werkzeuge mit, mit denen der Mensch eine laufende Sache abbricht.
        #
        # (Die Zahlen standen hier auf „vier Namen, drei Klassen" und nannten
        # `deep_task_status` als Kollateralschaden. Beides war ueberholt,
        # seit `research_quick` sichtbar ist — und `deep_research` stand nie
        # auf der Liste. Gemessen, nicht nachgerechnet.)
        tool.expose_to_llm = False
        hidden.append(name)
    log.info("tools.cognition_registered", mode=chosen, hidden=len(hidden))
    return sorted(hidden)


#: Wie viele zurueckliegende Vorfaelle der Diagnostiker hoechstens sieht.
DOCTOR_HISTORY = 20


async def _health_evidence(dispatcher) -> tuple[list[dict], list[dict]]:
    """Der Befundbogen des Diagnostikers — der letzte bekannte Stand, ohne Messung.

    Ausdruecklich `known()` und nicht `refresh()`: eine Fachauskunft soll keine
    Netzpruefungen ausloesen, und der Bogen sagt zu jedem Wert, wie alt er ist.
    Ein alter Wert, der als alt gekennzeichnet ist, ist ehrlicher als ein
    frischer, den eine Frage erzwungen hat.
    """
    doctor = getattr(dispatcher, "doctor", None)
    if doctor is None:
        return [], []
    components: list[dict] = []
    board = getattr(doctor, "board", None)
    if board is not None:
        try:
            components = [entry.as_dict() for entry in board.known()]
        except Exception as exc:  # noqa: BLE001
            log.info("bots.board_unreadable", kind=type(exc).__name__)
    events: list[dict] = []
    store = getattr(doctor, "store", None)
    if store is not None:
        try:
            events = list(await store.history(limit=DOCTOR_HISTORY))
        except Exception as exc:  # noqa: BLE001
            log.info("bots.doctor_history_unreadable", kind=type(exc).__name__)
    return components, events


async def tools_health(settings, dispatcher) -> dict:
    """Leichter Health-Status: HA-Erreichbarkeit, Tools, Spezialisten. Nur lokal/schnell."""
    ha_status = "NOT_CONFIGURED"
    if getattr(settings, "has_home_assistant", False):
        try:
            from solvio.integrations.home_assistant import HomeAssistant as _HA
            ok = await _HA(settings.home_assistant_url, settings.home_assistant_token).api_ok()
            ha_status = "CONNECTED" if ok else "ERROR"
        except Exception:  # noqa: BLE001
            ha_status = "ERROR"
    # Es gibt seit ADR-0028 nur noch EINE Naht zu externen Agenten-CLIs: den
    # Spezialistenstarter. Der alte `codex_agent` ist entfernt, also gibt es auch
    # keinen Agentenzustand mehr zu melden — gemeldet wird, ob die Werkzeuge
    # ueberhaupt dastehen. `resolve` schaut nur ins Dateisystem; eine
    # Anmeldeabfrage waere ein Unterprozess und hat in einem schnellen
    # Health-Aufruf nichts verloren (dafuer gibt es die Anbieter-Proben).
    from solvio.specialists.launcher import LauncherError, resolve
    specialists = {}
    for program in ("claude", "codex"):
        try:
            resolve(program)
            specialists[program] = "INSTALLED"
        except LauncherError as exc:
            specialists[program] = exc.reason.upper()
    return {
        "home_assistant": ha_status,
        "tools": "READY",
        "exposed_tools": [t["name"] for t in dispatcher.openai_tools()] if dispatcher else [],
        "specialists": specialists,
    }
