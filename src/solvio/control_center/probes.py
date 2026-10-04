"""Die tatsaechlichen Pruefungen — jede so billig wie moeglich, keine geraten.

Fuer jedes Teil wird der Weg genommen, den es ohnehin schon gibt: Home Assistant
hat `api_ok()`, die beiden Berater haben ihre eigenen Statusbefehle, der
Portal-Arbeiter meldet seinen Bauzustand beim Handschlag. Nichts davon ist neu
erfunden, und genau deshalb sagt es die Wahrheit.

Der teuerste Fall ist Google. Ein abgelaufener Zugang zeigt sich erst beim
Zugriff — es gibt kein Ablaufdatum, das man abfragen koennte, und eines zu
erfinden waere schlimmer als keines. Also wird eine winzige echte Abfrage
gemacht, hoechstens alle fuenf Minuten, und `invalid_grant` wird als das gelesen,
was es ist: **du musst dich neu anmelden**, nicht „kaputt".
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from solvio.control_center.health import (
    TTL_LOCAL, TTL_NETWORK, TTL_PROCESS, TTL_PROVIDER, Probe, State,
)
from solvio.logging_setup import get_logger

log = get_logger("control")

#: Wortlaute, an denen ein abgelaufener oder entzogener Zugang erkennbar ist.
#: Google nennt das `invalid_grant`; andere Anbieter schreiben es anders.
AUTH_MARKERS = ("invalid_grant", "unauthorized", "401", "invalid_credentials",
                "token_expired", "reauth", "credentials_missing", "not_configured",
                "no_refresh_token", "access_denied")

#: Wortlaute fuer ein erschoepftes Kontingent.
QUOTA_MARKERS = ("quota", "rate limit", "rate_limit", "429", "usage limit",
                 "too many requests", "exceeded")


def classify_error(text: str) -> tuple[State, str]:
    """Aus einer Fehlermeldung einen ehrlichen Zustand.

    Die Reihenfolge ist die Aussage: ein Anmeldeproblem zuerst, weil nur es einen
    Menschen braucht. Ein Kontingent danach, weil es von selbst vergeht. Alles
    andere ist eine Stoerung.
    """
    low = (text or "").lower()
    if any(marker in low for marker in AUTH_MARKERS):
        return State.AUTH_REQUIRED, "Zugang abgelaufen — bitte neu anmelden"
    if any(marker in low for marker in QUOTA_MARKERS):
        return State.QUOTA_LIMITED, "Kontingent gerade erschöpft"
    return State.UNAVAILABLE, (text or "nicht erreichbar")[:120]


def _recovers_in(service: Any) -> str:
    """Wann sich die Tageskappe erneuert — aus der Wahrheit des Brokers.

    Die Kappe ist ein UTC-KALENDERTAG (`session.utc_day`), kein rollendes
    24-Stunden-Fenster. Die Erholung ist deshalb ausrechenbar, und sie wird
    ausgerechnet statt geschaetzt: „erholt sich in etwa vier Stunden" ist eine
    Aussage, die jemand nachpruefen kann.
    """
    from solvio.provider_broker.session import seconds_until_utc_midnight
    try:
        moment = service._clock() if hasattr(service, "_clock") else time.time()
        seconds = seconds_until_utc_midnight(moment)
    except Exception:  # noqa: BLE001 - eine Probe wirft nie
        return "erholt sich zur naechsten UTC-Mitternacht"
    hours = seconds / 3600.0
    if hours >= 1.5:
        return f"erholt sich in rund {round(hours)} Stunden"
    return f"erholt sich in rund {max(1, round(seconds / 60.0))} Minuten"


def build(dispatcher: Any, settings: Any = None,
          approver: Any = None) -> list[Probe]:
    """Alle Pruefungen, die dieser Rechner heute ehrlich beantworten kann."""

    async def core() -> tuple[State, str]:
        # Wenn diese Zeile laeuft, laeuft der Core. Das ist keine tiefe Auskunft,
        # aber es ist die einzige, die hier ueberhaupt Sinn ergibt.
        return State.HEALTHY, f"läuft seit Prozessstart (pid {os.getpid()})"

    async def gateway() -> tuple[State, str]:
        runtime = approver or getattr(dispatcher, "approver_runtime", None)
        if runtime is None:
            return State.UNAVAILABLE, "kein Freigabeweg verdrahtet"
        try:
            pending = await runtime.approvals.pending()
        except Exception as exc:  # noqa: BLE001
            return State.DEGRADED, f"antwortet nicht ({type(exc).__name__})"
        waiting = len(pending)
        return State.HEALTHY, (f"erreichbar auf Port {runtime.port}"
                               + (f", {waiting} wartet" if waiting else ""))

    async def scheduler() -> tuple[State, str]:
        sched = getattr(dispatcher, "scheduler", None)
        if sched is None:
            return State.UNAVAILABLE, "Hintergrund läuft nicht"
        store = getattr(dispatcher, "proactive_store", None)
        if store is None:
            return State.DEGRADED, "kein Aufgabenspeicher"
        active = [t for t in await store.list_tasks(only_active=True)]
        return State.HEALTHY, (f"{len(active)} Aufgabe(n) aktiv"
                               if active else "bereit, nichts geplant")

    async def home_assistant() -> tuple[State, str]:
        url = (getattr(settings, "home_assistant_url", "") or "").strip() if settings else ""
        if not url:
            return State.UNKNOWN, "nicht eingerichtet"
        try:
            from solvio.capabilities import policy as _AP
            from solvio.integrations.home_assistant import HomeAssistant
            from solvio.secret_vault import context as _SC
            broker = getattr(dispatcher, "secret_broker", None)
            if broker is not None and broker.exists(HomeAssistant.CREDENTIAL_REF):
                client = HomeAssistant(url, broker=broker)
            else:
                client = HomeAssistant(url, getattr(settings, "home_assistant_token", ""))
            # Eine Gesundheitspruefung ist ein Hintergrundlauf ohne anwesenden
            # Menschen, und sie sagt das auch. Der HA-Eintrag im Tresor traegt
            # `home_assistant_health` in seiner Faehigkeitsliste — eine Pruefung,
            # die sich als Nutzerhandlung ausgaebe, waere ein Weg um die
            # Herkunftsbindung herum.
            with _SC.bound(_SC.UseContext(
                    origin=_AP.OriginClass.BACKGROUND_AUTOMATION,
                    capability="home_assistant_health",
                    automation_id="health.home_assistant")):
                ok = await client.api_ok()
        except Exception as exc:  # noqa: BLE001
            return classify_error(f"{type(exc).__name__}: {exc}")
        return (State.HEALTHY, "erreichbar") if ok else (State.UNAVAILABLE,
                                                         "antwortet nicht")

    #: Kalender und E-Mail haengen an DENSELBEN Google-Zugangsdaten. Zwei
    #: getrennte Pruefungen kosten doppelt und koennen sich sogar widersprechen,
    #: wenn die eine den Zwischenspeicher des Zugriffstokens waermt und die
    #: andere dadurch gruen wird. Also wird einmal gefragt und beides gesetzt.
    google_shared: dict[str, Any] = {}

    def _provider_probe(capability: str, arguments: dict[str, Any], label: str):
        async def check() -> tuple[State, str]:
            router = getattr(dispatcher, "capabilities", None)
            if router is None or capability not in set(router.names()):
                return State.UNKNOWN, "nicht eingerichtet"
            # Hat die Schwesterpruefung gerade schon gefragt, gilt ihre Antwort.
            recent = google_shared.get("result")
            if recent is not None and time.time() - google_shared.get("at", 0) < 60:
                return recent
            from solvio.capabilities.contract import ArgumentSource
            from solvio.capabilities.policy import OriginClass
            from solvio.contracts.trust import TrustContext, TrustLevel
            result = await router.execute(
                capability, arguments,
                trust=TrustContext(origin_trust=TrustLevel.SYSTEM_TRUSTED,
                                   user_authorized=False,
                                   note="control center health probe"),
                provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in arguments},
                principal="control-center",
                # Eine Gesundheitsprobe laeuft ohne anwesenden Menschen. Sie
                # kann ohnehin nichts schreiben (`user_authorized=False`), aber
                # das Journal soll die Herkunft benennen statt „unbekannt" zu
                # sagen ueber einen Pfad, den der Core selbst besitzt.
                origin=OriginClass.BACKGROUND_AUTOMATION)
            if result.succeeded:
                google_shared.update(result=(State.HEALTHY, "Zugang gültig"),
                                     at=time.time())
                return State.HEALTHY, "Zugang gültig"
            # Der wichtigste Zweig dieses Moduls: `invalid_grant` heisst „melde
            # dich neu an", nicht „kaputt". Wer das zusammenwirft, schickt den
            # Nutzer auf Fehlersuche statt in den Anmeldedialog.
            # `detail` traegt die Kennung des Anbieters — etwa `invalid_grant`.
            # `reason` und `human_message` sind absichtlich allgemein gehalten,
            # und genau deshalb reichen sie hier nicht: gemessen an einer echten
            # Google-Antwort wurde aus „melde dich neu an" eine „Stoerung", und
            # der Nutzer bekam „ich weiss noch nicht, warum" statt des
            # Anmeldedialogs. Die Kennung enthaelt kein Geheimnis; der Token
            # taucht dort ausdruecklich nie auf.
            verdict = classify_error(
                f"{result.reason} {result.detail} {result.human_message}")
            if verdict[0] is State.UNAVAILABLE and result.human_message:
                # Im Stoerungsfall den lesbaren Satz zeigen, nicht das Rohgemisch.
                verdict = (State.UNAVAILABLE, result.human_message)
            google_shared.update(result=verdict, at=time.time())
            return verdict
        return check

    async def hermes() -> tuple[State, str]:
        """Lebt der Prozess — nicht: ist er verdrahtet.

        Der erste Entwurf hier meldete „bereit", sobald `deep_research`
        registriert war. Das ist Verdrahtung, keine Lebendigkeit: der Prozess im
        Gefaengnis kann laengst gestorben sein, und die Registrierung bliebe
        stehen. Genau die Sorte gruener Punkt, die dieser Meilenstein abschaffen
        soll.

        Geprueft wird deshalb der Rueckgabewert des Kindprozesses — `None` heisst
        „laeuft noch". Das ist oertlich und kostet nichts.
        """
        runtime = getattr(dispatcher, "deep_runtime", None)
        if runtime is None:
            return State.UNAVAILABLE, "Recherche läuft nicht"
        process = getattr(runtime, "process", None)
        if process is None:
            return State.DEGRADED, "kein isolierter Prozess"
        alive = getattr(process, "alive", None)
        if callable(alive) and not alive():
            return State.UNAVAILABLE, "Prozess beendet"
        router = getattr(dispatcher, "capabilities", None)
        if router is None or "deep_research" not in set(router.names()):
            return State.DEGRADED, "läuft, aber nicht verdrahtet"
        return State.HEALTHY, "bereit"

    async def broker() -> tuple[State, str]:
        """Steht der Broker — und ist heute noch Luft in der Tokenkappe?

        Der Broker gehoert dem Core und laeuft in dessen Prozess; es gibt
        deshalb bewusst KEIN Playbook, das ihn allein neu startet. Was diese
        Sonde melden kann, ist der Zustand, nicht die Reparatur: laeuft er
        nicht, verweigern Deep und die Bots — und das soll man sehen, statt es
        aus drei roten Punkten zu erraten.
        """
        service = getattr(dispatcher, "provider_broker", None)
        if service is None:
            return State.UNAVAILABLE, "Anbieter-Vermittlung nicht eingerichtet"
        if not service.listening():
            return State.UNAVAILABLE, "Anbieter-Vermittlung lauscht nicht"
        capped = []
        for name in service.registry.names():
            principal = service.registry.principal(name)
            if principal is None:
                continue
            if principal.tokens_today >= principal.caps.tokens_per_day:
                capped.append(name)
        if capped:
            # QUOTA_LIMITED, nicht DEGRADED — und das ist kein Etikettentausch.
            # `DEGRADED` fuehrte den Arzt in seinen Ausfall-Zweig: er meldete
            # „ist seit X nicht erreichbar, dafuer habe ich kein Mittel",
            # obwohl der Broker vollstaendig gesund lauscht und sich die Lage
            # in der Nacht von selbst erledigt. Der Kontingent-Zweig des
            # Arztes (NO_ACTION, „das Kontingent erneuert sich von selbst")
            # existierte und war bis hierher unerreichbar.
            return (State.QUOTA_LIMITED,
                    f"Tagesgrenze erreicht: {', '.join(capped)} — "
                    f"{_recovers_in(service)}")
        return State.HEALTHY, "bereit"

    async def claude() -> tuple[State, str]:
        from solvio.specialists.providers import claude_status
        status = await claude_status()
        if status.available:
            return State.HEALTHY, f"angemeldet ({status.auth})"
        if status.reason == "logged_out":
            return State.AUTH_REQUIRED, "abgemeldet — bitte neu anmelden"
        if status.reason == "not_installed":
            return State.UNKNOWN, "nicht installiert"
        return State.UNAVAILABLE, status.reason or "nicht erreichbar"

    async def codex() -> tuple[State, str]:
        from solvio.specialists.providers import codex_status
        status = await codex_status()
        if status.available:
            return State.HEALTHY, f"angemeldet ({status.auth})"
        if status.reason == "logged_out":
            return State.AUTH_REQUIRED, "abgemeldet — bitte neu anmelden"
        if status.reason == "not_installed":
            return State.UNKNOWN, "nicht installiert"
        return State.UNAVAILABLE, status.reason or "nicht erreichbar"

    async def satellite() -> tuple[State, str]:
        """Das Ohr. Die einzige Pruefung hier, die NICHT selbst nachsieht.

        Der Core kann den Pi nicht befragen, ohne eine Fernverwaltung zu
        bekommen, die es bewusst noch nicht gibt. Also berichtet der Satellit
        von sich aus ueber den bereits authentifizierten Weg, und hier wird nur
        gelesen, was zuletzt ankam — samt der Frage, wie alt das ist. Ein alter
        Bericht mit dem Wort `healthy` ist keine Auskunft ueber jetzt. Wer sich
        nie gemeldet hat, ist `unknown`; wer sich gemeldet hat und verstummt
        ist, ist `unavailable` und zaehlt damit als Stoerung. Dass Schweigen wie
        Gesundheit aussah, war der ganze Defekt.
        """
        from solvio.realtime.satellite_health import state_for
        registry = getattr(dispatcher, "satellite_health", None)
        if registry is None:
            return State.UNKNOWN, "kein Satellitenkanal verdrahtet"
        state, reason = state_for(registry.latest())
        return State(state), reason

    async def storage() -> tuple[State, str]:
        """Die externe Sicherungsplatte — und die Sicherung, die auf ihr liegt.

        Die einzige Pruefung im Haus, die GESUND meldet, waehrend das Geraet
        fehlt. Das ist Absicht und der Kernsatz des Speicher-Milestones: eine
        abgezogene Platte ist kein Defekt, solange die letzte Sicherung frisch
        ist. Erst wenn daraus ein Risiko wird, faerbt sie sich.

        Die Zuordnung selbst steht in `storage.health` — hier wird nur
        uebersetzt, damit das Kontrollzentrum nichts ueber Datentraeger wissen
        muss.
        """
        try:
            from solvio.storage.health import assess
            word, reason = await asyncio.to_thread(assess)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Speicher nicht messbar"

    async def offsite() -> tuple[State, str]:
        """Die Sicherung AUSSERHALB des Hauses — gegen Feuer, Wasser, Diebstahl.

        Sie steht neben „Sicherung", nicht darin: die externe Platte liegt im
        selben Raum wie der Mac, und gegen einen Brand hilft sie nicht.

        Der eine Satz, an dem diese Sonde haengt: **ein Upload-Erfolg macht
        NIE gruen.** `healthy` verlangt zusaetzlich einen frischen
        Restore-Beweis — sonst waere „ich habe etwas hingelegt" dasselbe wie
        „ich koennte es zurueckholen", und genau das ist der Unterschied
        zwischen einem Objekt und einem Backup.

        Es gibt fuer diese Pruefung bewusst **KEIN Playbook**. Jede denkbare
        Reparatur ist entweder Menschensache (ein gesperrter Schluesselbund,
        ein fehlender Zugang) oder gefaehrlich (loeschen, neu schluesseln) —
        und SOLVIO hat beim Anbieter ohnehin kein Loeschrecht. Der Arzt darf
        das melden; wegreparieren kann er es nicht.

        Bewertet wird ausschliesslich aus OERTLICHEN Quellen (Offsite-Buch +
        state.json), damit die Auskunft auch ohne Netz gilt.
        """
        try:
            from solvio.storage.offsite.health import assess
            word, reason = await asyncio.to_thread(assess)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Offsite nicht messbar"

    async def vault() -> tuple[State, str]:
        """Der Geheimnistresor — geprueft, ohne ihn aufzumachen.

        Nichts hier entschluesselt einen Zugang. Geprueft werden Dateiheiligkeit,
        Rechte, Erreichbarkeit des Hauptschluessels, Aktualitaet des
        Wiederherstellungsumschlags und ob irgendwo noch eine Klartextdublette
        liegt. Ein Zustandsbericht, der jeden Zugang oeffnet, um zu sagen, dass
        es sie gibt, waere selbst ein Angriffspfad — und er liefe alle fuenfzehn
        Sekunden.

        `asyncio.to_thread`, weil der Schluesselbund ueber `subprocess` mit einer
        Frist von acht Sekunden befragt wird. Auf dem Ereignisschleifen-Thread
        waere ein gesperrter Schluesselbund eine achtsekuendige Stille in der
        laufenden Sprachsitzung.
        """
        try:
            from solvio.secret_vault.health import assess
            word, reason = await asyncio.to_thread(assess)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Tresor nicht messbar"

    async def payment() -> tuple[State, str]:
        """Die Zahlungsschicht — geprueft, ohne einen Cent zu bewegen.

        Nichts hier belastet, nichts hier leiht einen Anbieterzugang aus. Eine
        Gesundheitspruefung, die eine Testbuchung macht, ist keine Pruefung,
        sondern ein Dauerauftrag — und sie liefe alle fuenfzehn Sekunden.

        `asyncio.to_thread`, weil eine SQLite-Datei blockieren kann; auf dem
        Ereignisschleifen-Thread waere das eine Stille in der laufenden
        Sprachsitzung. Derselbe Grund wie beim Tresor.
        """
        try:
            from solvio.payment.health import assess
            word, reason = await asyncio.to_thread(assess)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Zahlungen nicht messbar"

    async def portal() -> tuple[State, str]:
        socket_path = "/var/solvio-portal/run/portal.sock"
        if not os.path.exists(socket_path):
            return State.UNAVAILABLE, "Arbeiter läuft nicht"
        return State.HEALTHY, "Arbeiter erreichbar"

    async def browser() -> tuple[State, str]:
        try:
            from solvio.browser.cdp import chrome_binary
            return ((State.HEALTHY, "bereit") if chrome_binary()
                    else (State.UNAVAILABLE, "kein Browser gefunden"))
        except Exception:  # noqa: BLE001
            return State.UNAVAILABLE, "kein Browser gefunden"

    async def autopilot() -> tuple[State, str]:
        """Der Entwicklungs-Autopilot. Lesend — er repariert hier nichts.

        Wie bei der Fernsicherung gibt es bewusst KEIN Reparatur-Playbook: ein
        Milestone, der auf den Menschen wartet, wird nicht durch einen
        Neustart gesund.
        """
        try:
            from solvio.autopilot.status import assess
            wort, grund = await asyncio.to_thread(assess)
            return State(wort), grund
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Autopilot nicht messbar"

    async def agent_runtime() -> tuple[State, str]:
        """Die Agentenlaufzeit — offene, wartende und steckende Auftraege.

        Ein Lauf, der auf einen Menschen wartet, ist kein Defekt und faerbt
        nicht; er wird gezaehlt und genannt. Ein STECKENDER faerbt schon: „seit
        ueber einer Stunde ohne Regung" ist die Definition, und ein Lauf, den
        niemand als steckend sieht, ist genau der, der niemandem auffaellt.
        """
        try:
            from solvio.agent_runtime.health import assess
            word, reason = await asyncio.to_thread(assess)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Agentenlaufzeit nicht messbar"

    async def cognition() -> tuple[State, str]:
        """Der kognitive Router — Einordnungen, Fehlschlaege, Eskalationen.

        Er gehoert dem Core und laeuft in dessen Prozess; es gibt deshalb
        bewusst KEIN Playbook, das ihn allein neu startet. Was diese Sonde
        melden kann, ist der Zustand, nicht die Reparatur.

        Ist er abgeschaltet, meldet sie `unknown` und nicht `healthy`: „laeuft
        nicht" und „laeuft gut" sind zwei verschiedene Auskuenfte.
        """
        try:
            from solvio.cognition.health import assess
            mode = str(getattr(dispatcher, "cognitive_router_mode", "off"))
            word, reason = await asyncio.to_thread(assess, mode=mode)
            return State(word), reason
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Kognitiver Router nicht messbar"

    async def remote_access() -> tuple[State, str]:
        """Steht der Fernweg — also waere das Telefon von unterwegs bei mir?

        Gemessen wird das eine Stueck, das ausfallen kann, ohne dass der Mac
        etwas davon merkt: die Strecke zum Vermittler. Der lokale Weg wird
        NICHT gemessen — der Core ist der lokale Weg, und eine Sonde, die sich
        selbst anruft, sagt nur, dass sie laeuft.

        Ein TCP-Verbindungsaufbau, kein Byte Nutzlast, keine Kennung. Faellt er
        aus, ist das ein Zustand und kein Notfall: SOLVIO laeuft im Haus
        vollstaendig weiter, und der Eigentuemer bekommt eine ehrliche Auskunft
        statt eines Zeitablaufs auf dem Telefon.

        Nicht eingerichtet ist `unknown`, nicht `healthy`. „Kein Fernweg
        vorgesehen" und „Fernweg gesund" sind zwei verschiedene Auskuenfte.
        """
        try:
            from solvio.remote_access.config import endpoints_from_environment
            from solvio.remote_access.contract import ConnectionState
            from solvio.remote_access.resolver import LocalFirstResolver
            from solvio.remote_access.tcp_probe import TcpProbeTransport
        except Exception:  # noqa: BLE001
            return State.UNKNOWN, "Fernzugang nicht messbar"
        try:
            endpoints = endpoints_from_environment()
        except Exception as exc:  # noqa: BLE001
            # Eine kaputte Angabe ist laut. Sie darf nur nicht die Tafel
            # mitnehmen — deshalb wird sie hier zu einem Zustand.
            return State.DEGRADED, f"Fernzugang falsch eingerichtet ({type(exc).__name__})"
        if not endpoints:
            return State.UNKNOWN, "nicht eingerichtet"
        report = await LocalFirstResolver(TcpProbeTransport("wireguard"),
                                          endpoints).resolve()
        if report.state is ConnectionState.REMOTE_UNAVAILABLE:
            return State.UNAVAILABLE, "von unterwegs gerade nicht erreichbar"
        return State.HEALTHY, "von unterwegs erreichbar"

    return [
        Probe("core", "SOLVIO", core, ttl=TTL_LOCAL),
        Probe("cognition", "Einordnung", cognition, ttl=TTL_LOCAL),
        Probe("agent_runtime", "Auftraege", agent_runtime, ttl=TTL_LOCAL),
        Probe("autopilot", "Entwicklung", autopilot, ttl=TTL_LOCAL),
        Probe("gateway", "Freigaben", gateway, ttl=TTL_LOCAL),
        Probe("scheduler", "Hintergrund", scheduler, ttl=TTL_LOCAL),
        Probe("calendar", "Kalender",
              _provider_probe("calendar_list_events", {"when": "heute"},
                              "Kalender"),
              ttl=TTL_PROVIDER, network=True),
        Probe("gmail", "E-Mail",
              _provider_probe("gmail_list_recent", {"limit": 1}, "E-Mail"),
              ttl=TTL_PROVIDER, network=True),
        Probe("home_assistant", "Zuhause", home_assistant,
              ttl=TTL_NETWORK, network=True),
        Probe("hermes", "Recherche", hermes, ttl=TTL_PROCESS),
        Probe("broker", "Anbieter-Vermittlung", broker, ttl=TTL_PROCESS),
        Probe("claude", "Architekt", claude, ttl=TTL_PROVIDER),
        Probe("codex", "Herausforderer", codex, ttl=TTL_PROVIDER),
        Probe("satellite", "Ohr", satellite, ttl=TTL_LOCAL),
        Probe("storage", "Sicherung", storage, ttl=TTL_LOCAL),
        Probe("offsite", "Fernsicherung", offsite, ttl=TTL_LOCAL),
        Probe("vault", "Tresor", vault, ttl=TTL_LOCAL),
        Probe("payment", "Zahlungen", payment, ttl=TTL_LOCAL),
        Probe("portal", "Portale", portal, ttl=TTL_LOCAL),
        Probe("browser", "Browser", browser, ttl=TTL_PROCESS),
        Probe("remote_access", "Fernzugang", remote_access,
              ttl=TTL_NETWORK, network=True),
    ]
