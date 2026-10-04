"""Der Broker selbst — Steuerebene im Prozess, Datenebene auf der Rueckschleife.

**Die Steuerebene hat keine Route.** `register_principal`, `open_lease` und
`close_lease` sind Python-Aufrufe an einem Handle, das beim Verdrahten
weitergereicht wird. Es gibt keinen HTTP-Weg, auf dem sich der Kaefig ein Lease
oeffnen, einen Auftraggeber anlegen oder einen Token praegen koennte. Das ist
die tragende Trennung dieses Milestones: der Kaefig darf Inferenz **einreichen**
und sonst nichts.

**Die Datenebene bindet ausschliesslich `127.0.0.1:8792`** — nicht `0.0.0.0`,
nicht die LAN-Adresse. Was dort ankommt, laeuft durch die Torfolge in
`proxy.py`.

**Der Broker nimmt die Stimme nicht mit.** Bindet der Port nicht, wird laut
protokolliert, `listening()` bleibt falsch, und der Core laeuft weiter — Deep
und die Bots verweigern dann ueber genau dieses Tor. Ein Broker, der den Core
mitnaehme, waere die schlechtere Schuld.

**Der Broker bekommt kein Doktor-Playbook.** Ein Alleinneustart leerte die
Registratur, waehrend alle vier Kaefig-`.env` ihre alten Token behielten, und
niemand praegte sie nach. Er steht deshalb in `FORBIDDEN_RESTARTS` und startet
mit dem Core.
"""
from __future__ import annotations

import asyncio
import errno
import os
import time
from typing import Any, Callable

from solvio.logging_setup import get_logger
from solvio.provider_broker import proxy as px
from solvio.provider_broker import upstream as up
from solvio.provider_broker.ledger import BrokerLedger
from solvio.provider_broker import anthropic as an
from solvio.provider_broker.session import (ADAPTIVE_EXTRACTOR_CAPS, AGENT_CAPS,
                                            AGENT_ESCALATION_CAPS,
                                            AUTOPILOT_LEAD_CAPS,
                                            AUTOPILOT_LEAD_ESCALATION_CAPS,
                                            AUTOPILOT_WRITER_CLAUDE_CAPS,
                                            AUTOPILOT_WRITER_CLAUDE_ESCALATION_CAPS,
                                            BOT_CAPS, COGNITION_CAPS,
                                            COGNITION_ESCALATION_CAPS,
                                            DEEP_CAPS, NEXUS_WORKER_CLAUDE_CAPS,
                                            RESEARCH_QUICK_CAPS,
                                            VOICE_SUPERVISOR_CAPS,
                                            DOCUMENT_CAPS, DOCUMENT_PRINCIPAL,
                                            CapExceeded, Registry,
                                            seconds_until_utc_midnight,
                                            utc_day, utc_midnight)

log = get_logger("broker")

DEFAULT_PORT = 8792
PORT_ENV = "SOLVIO_BROKER_PORT"
BIND_HOST = "127.0.0.1"

#: Wie lange ein Bind auf den eigenen Vorgaenger warten darf. Kurz und endlich:
#: sie ueberbrueckt einen Neustart, sie beugt sich keinem Besetzer.
BIND_GRACE = 12.0
BIND_RETRY_PAUSE = 1.0

#: Der Name des Auftraggebers fuer das Deep-Gateway. Die Botprofile heissen
#: `bot:<profil>`.
DEEP_PRINCIPAL = "deep-gateway"

#: Der Planer der Agentenlaufzeit — eigener Auftraggeber, eigene Kappen,
#: eigene Zeile im Buch. Die Laufzeit bekommt KEINEN Anbieterschluessel.
AGENT_PRINCIPAL = "agent-runtime"

#: Der kognitive Router — eigener Auftraggeber, eigene Kappen, eigene Zeile im
#: Buch. Er darf `gpt-5.4-mini`, sonst nichts.
COGNITION_PRINCIPAL = "cognitive-router"

#: Die zwei Eskalations-Auftraggeber. Sie sind der EINZIGE Weg zu `gpt-5.4`,
#: und ihre Token haelt ausschliesslich Core-Code — kein Kaefig, kein
#: Spezialist, kein Fremdprozess sieht sie je. Ein Modell kann eine Eskalation
#: empfehlen; ausloesen kann sie nur die Politik, die diese Namen kennt.
COGNITION_ESCALATION_PRINCIPAL = "cognitive-router-escalation"
AGENT_ESCALATION_PRINCIPAL = "agent-runtime-escalation"

#: Wie lange ein Client nach einer Ratenbremse warten soll. Kurz und fest —
#: `rate_capped` entsteht an gleichzeitigen Anfragen, nicht an einem Tagesstand.
RATE_RETRY_AFTER = 30

#: Welche Ablehnung ein ehrliches `Retry-After` traegt, und woher die Zahl
#: kommt. Was hier NICHT steht, bekommt bewusst keines:
#:
#: * `lease_budget_exhausted` / `lease_request_capped` — das Budget DIESES
#:   Auftrags fuellt sich nie nach. Eine Wartezeit zu nennen waere gelogen.
#: * `duplicate_request` — es gibt nichts zu wiederholen; die Antwort war
#:   schon da.
#: * `bad_token`, `lease_absent`, `path_not_allowed`, … — Wartezeit hilft nicht.
_RETRY_AFTER = {
    # Die Tageskappe ist ein UTC-KALENDERTAG (`utc_day`/`_roll_day`), kein
    # rollendes 24-Stunden-Fenster. Die Erholung ist deshalb ausrechenbar, und
    # der Broker rechnet sie aus, statt sie zu schaetzen.
    "token_capped": lambda now: seconds_until_utc_midnight(now),
    "rate_capped": lambda now: RATE_RETRY_AFTER,
}

#: Der Technical Lead des Entwicklungs-Autopiloten und seine Eskalationsstufe.
#: Eigener Auftraggeber, eigene Kappen, eigene Zeile im Buch — damit ein
#: Builder-Kontingent den Lead nicht mitreisst und umgekehrt.
AUTOPILOT_LEAD_PRINCIPAL = "autopilot-lead"
AUTOPILOT_LEAD_ESCALATION_PRINCIPAL = "autopilot-lead-escalation"

#: Der schreibende Claude-Builder und seine Eskalationsstufe (V0.6). Eigene
#: Auftraggeber, eigene Kappen, eigene Modell-Allowlist — und sie sprechen die
#: ANTHROPIC-Flaeche, nicht die OpenAI-Flaeche.
AUTOPILOT_WRITER_CLAUDE_PRINCIPAL = "autopilot-writer-claude"
AUTOPILOT_WRITER_CLAUDE_ESCALATION_PRINCIPAL = "autopilot-writer-claude-escalation"

#: N8/C4: der Claude-Auftragsarbeiter des Nexus-Auftragswegs (`worker/claude`).
#: Eigener Auftraggeber, eigene Kappen, eigene Faehigkeit im Tresor
#: (`nexus.claude_worker`), Lease `ref="task:<task_id>"` je Turn.
NEXUS_WORKER_CLAUDE_PRINCIPAL = "nexus-worker-claude"

#: Genau die Auftraggeber, die auf der Anthropic-Flaeche ueberhaupt etwas
#: duerfen. Eine geschlossene Menge: ein Deep- oder Lead-Token, das dort
#: auftaucht, ist eine Absage — nicht ein Sonderfall, den jemand bedacht
#: haben muss.
ANTHROPIC_PRINCIPALS = frozenset({AUTOPILOT_WRITER_CLAUDE_PRINCIPAL,
                                  AUTOPILOT_WRITER_CLAUDE_ESCALATION_PRINCIPAL,
                                  NEXUS_WORKER_CLAUDE_PRINCIPAL})

#: Der Adaptiv-Extraktor (DEBT-0145). Eigener Auftraggeber, eigene Kappen,
#: eigene Zeile im Buch — derselbe Grenzweg wie jeder andere Aufrufer im Haus,
#: statt eines direkten Anbieterschluessels im Modul.
ADAPTIVE_EXTRACTOR_PRINCIPAL = "adaptive-extractor"

#: Die Kurzrecherche im selben Gespraechs-Turn — eigener Auftraggeber, eigene
#: Kappen, eigene Zeile im Buch, und der EINZIGE, der `web_search` (das
#: anbieterseitige Werkzeug) ueberhaupt nennen darf.
RESEARCH_QUICK_PRINCIPAL = "research-quick"

#: Der starke Antwortweg im Sprachgespraech -- eigener Auftraggeber, eigene
#: Kappen, eigene Zeile im Buch, und der VIERTE (und einzige nicht
#: eskalierende), der `gpt-5.4` anfordern darf. Er darf KEIN anbieterseitiges
#: Werkzeug nennen.
VOICE_SUPERVISOR_PRINCIPAL = "voice-supervisor"

ALLOWED_DOCUMENT_MIME = frozenset({"application/pdf", "image/png", "image/jpeg",
                                   "image/webp", "image/gif"})


def bot_principal(profile: str) -> str:
    return f"bot:{profile}"


def configured_port() -> int:
    raw = (os.environ.get(PORT_ENV, "") or "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


#: Der in DIESEM Prozess laufende Broker — fuer Core-eigene Aufrufer, die
#: kein Griff des Dispatchers erreicht (der native Auftragsweg, das
#: Kostentor des Claude-Arbeiters). Kein zweiter Broker, keine Route: nur
#: der Verweis auf den einen, der `start()` durchlaufen hat und lauscht.
_RUNNING: "BrokerService | None" = None


def running() -> "BrokerService | None":
    """Der lauschende Broker dieses Prozesses, sonst None."""
    broker = _RUNNING
    return broker if broker is not None and broker.listening() else None


class BrokerService:
    """Registratur, Leases, Kappen, Buch und die Rueckschleifen-Datenebene."""

    def __init__(self, *, provider_key: str, port: int = 0,
                 ledger_path: str = "", clock: Callable[[], float] | None = None,
                 vault: Any = None) -> None:
        self.port = port or configured_port()
        self.registry = Registry()
        self.ledger = BrokerLedger(ledger_path)
        self.upstream = up.Upstream(provider_key)
        #: Die zweite Flaeche. Sie haelt keinen Wert — sie leiht ihn je Anfrage
        #: aus dem Tresor. Ohne Tresor ist sie `configured() == False`, und das
        #: ist eine ehrliche Lage (`no_credential`), kein Absturz.
        self.anthropic = an.AnthropicUpstream(vault)
        self._clock = clock or time.time
        self._runner: Any = None
        self._site: Any = None
        self._listening = False
        self._buffered = 0
        self._inflight: dict[str, set[px.Forward]] = {}
        self._writers: dict[str, Callable[[str], None]] = {}
        self._lock = asyncio.Lock()

    # ---- Lebenszyklus --------------------------------------------------

    async def start(self) -> None:
        """Bindet die Datenebene. Wirft, wenn der Port nicht zu haben ist.

        Der Aufrufer faengt das — `EADDRINUSE` eingeschlossen. Ein belegter Port
        heisst: irgendjemand anderes ist dort, und dann wird ganz sicher keine
        Kaefig-`.env` geschrieben.
        """
        from aiohttp import web

        if not self.upstream.configured():
            raise RuntimeError("provider broker has no provider credential")

        self.ledger.open()

        # aiohttp muss den groessten zugelassenen Rumpf bis zu unserem
        # auftraggebergebundenen Lesetor durchlassen. Fuer alle anderen gilt
        # dort weiterhin unveraendert MAX_BODY_BYTES.
        app = web.Application(client_max_size=px.MAX_DOCUMENT_BODY_BYTES + (1 << 20))
        app.router.add_route("POST", "/v1/responses", self._handle_forward)
        app.router.add_route("POST", "/v1/chat/completions", self._handle_forward)
        app.router.add_route("GET", px.MODELS_PATH, self._handle_models)
        for pfad in an.FORWARDED_PATHS:
            app.router.add_route("POST", pfad, self._handle_anthropic)
        # Alles andere — die Abdrucksonden eingeschlossen — endet hier, lokal
        # und ohne jede Beruehrung des Anbieters.
        app.router.add_route("*", "/{tail:.*}", self._handle_denied)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # `reuse_address` bleibt auf der Vorgabe, und das ist eine Korrektur aus
        # der Live-Abnahme. Der erste Entwurf setzte es auf `False`, um einen
        # Platzbesetzer streng zu erkennen — es leistet dafuer aber nichts: gegen
        # einen LEBENDEN Lauscher scheitert der Bind ohnehin, dafuer braeuchte
        # ein Angreifer `SO_REUSEPORT`. Bezahlt hat es etwas anderes: nach einem
        # `launchd`-Neustart hielt der eigene Vorgaenger den Socket noch, der
        # Bind scheiterte mit `EADDRINUSE`, und Deep und die Bots blieben eine
        # ganze Core-Lebenszeit unten. Genau das ist live passiert.
        await self._bind_with_grace()
        self._listening = True
        global _RUNNING
        _RUNNING = self
        log.info("broker.started", port=self.port, host=BIND_HOST)

    async def _bind_with_grace(self) -> None:
        """Bindet — und gibt dem eigenen Vorgaenger einen Moment.

        Kein Zugestaendnis an einen Platzbesetzer: die Frist ist kurz und
        endlich, und wer danach noch dort liegt, laesst den Start scheitern.
        Sie deckt genau den Fall ab, in dem `launchd` den Nachfolger startet,
        bevor der Socket des Vorgaengers geschlossen ist.
        """
        from aiohttp import web

        deadline = time.monotonic() + BIND_GRACE
        while True:
            # Ein `TCPSite` laesst sich nicht zweimal starten; jeder Versuch
            # bekommt deshalb einen frischen.
            site = web.TCPSite(self._runner, BIND_HOST, self.port)
            try:
                await site.start()
                self._site = site
                return
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                    raise
                log.info("broker.bind_retry", port=self.port)
                await asyncio.sleep(BIND_RETRY_PAUSE)

    async def stop(self) -> None:
        global _RUNNING
        self._listening = False
        if _RUNNING is self:
            _RUNNING = None
        for name in list(self._inflight):
            self._abort_principal(name)
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                log.info("broker.stop_unclean", kind=type(exc).__name__)
            self._runner = None
            self._site = None
        self.ledger.close()
        log.info("broker.stopped")

    def listening(self) -> bool:
        return self._listening

    # ---- Steuerebene (in-Prozess, KEINE Route) -------------------------

    def register_principal(self, name: str) -> str:
        """Praegt und registriert einen Token fuer diesen Auftraggeber.

        Die Generation zaehlt **je Auftraggeber**. Ein `doctor hermes_restart`
        praegt nur `deep-gateway` neu; die drei Bot-Token bleiben gueltig.
        """
        caps = (BOT_CAPS if name.startswith("bot:")
                else AGENT_CAPS if name == AGENT_PRINCIPAL
                else AGENT_ESCALATION_CAPS if name == AGENT_ESCALATION_PRINCIPAL
                else COGNITION_CAPS if name == COGNITION_PRINCIPAL
                else COGNITION_ESCALATION_CAPS
                if name == COGNITION_ESCALATION_PRINCIPAL
                else AUTOPILOT_LEAD_CAPS if name == AUTOPILOT_LEAD_PRINCIPAL
                else AUTOPILOT_LEAD_ESCALATION_CAPS
                if name == AUTOPILOT_LEAD_ESCALATION_PRINCIPAL
                else AUTOPILOT_WRITER_CLAUDE_CAPS
                if name == AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
                else AUTOPILOT_WRITER_CLAUDE_ESCALATION_CAPS
                if name == AUTOPILOT_WRITER_CLAUDE_ESCALATION_PRINCIPAL
                else NEXUS_WORKER_CLAUDE_CAPS
                if name == NEXUS_WORKER_CLAUDE_PRINCIPAL
                else ADAPTIVE_EXTRACTOR_CAPS
                if name == ADAPTIVE_EXTRACTOR_PRINCIPAL
                else VOICE_SUPERVISOR_CAPS
                if name == VOICE_SUPERVISOR_PRINCIPAL
                else RESEARCH_QUICK_CAPS
                if name == RESEARCH_QUICK_PRINCIPAL
                else DOCUMENT_CAPS if name == DOCUMENT_PRINCIPAL
                else DEEP_CAPS)
        token = self.registry.register(name, caps=caps)
        principal = self.registry.principal(name)
        if principal is not None:
            # Ein Core-Neustart darf die Tageskappe nicht zuruecksetzen: die
            # Registratur ist fluechtig, das Buch nicht.
            moment = self._clock()
            midnight = _utc_midnight(moment)
            used = self.ledger.usage_since(principal=name, since=midnight)
            self.registry.seed_day(principal, requests=used["requests"],
                                   tokens=used["tokens"], now=moment)
        return token

    def set_credential_writer(self, name: str, writer: Callable[[str], None]) -> None:
        """Wer die `.env` dieses Auftraggebers besitzt, traegt sich hier ein.

        Bei Lease-Null praegt der Broker neu und meldet den frischen Token
        hierher. **Reihenfolge:** erst praegen und registrieren, dann die Datei
        ersetzen. Scheitert das Schreiben, bleibt der alte Token ungueltig — ein
        Ausfall ist besser als ein weiterverwendbarer Zugang.
        """
        self._writers[name] = writer

    def open_lease(self, principal: str, ref: str, *, deadline: float,
                   max_tokens: int | None = None,
                   max_requests: int | None = None) -> str:
        """Oeffnet ein Zeitfenster — auf Wunsch mit Budget fuer diesen Auftrag.

        Auch das ist ein Python-Aufruf ohne Route. Es gibt keinen Kopfsatz,
        kein Rumpffeld und keine Umgebungsvariable, mit der sich der Kaefig ein
        Budget ausstellen oder ein bestehendes anheben koennte.
        """
        return self.registry.open_lease(principal, ref, deadline=deadline,
                                        now=self._clock(), max_tokens=max_tokens,
                                        max_requests=max_requests)

    def close_lease(self, lease_id: str) -> None:
        """Schliesst ein Lease. Faellt der Auftraggeber auf null, rotiert er.

        Dieser Aufruf steht in einem `finally` und darf deshalb **nie** werfen.
        """
        try:
            drained = self.registry.close_lease(lease_id, now=self._clock())
        except Exception as exc:  # noqa: BLE001
            log.error("broker.lease_close_failed", kind=type(exc).__name__)
            return
        if drained:
            self._rotate(drained)

    def rotate_now(self, name: str) -> None:
        """Von aussen erzwungene Rotation — fuer Tests und die Abnahme."""
        self._rotate(name)

    def _rotate(self, name: str) -> None:
        """Schicht 3: kein Zugang ueber den Auftrag hinaus."""
        try:
            token = self.registry.register(name)
        except Exception as exc:  # noqa: BLE001
            log.error("broker.rotate_failed", principal=name,
                      kind=type(exc).__name__)
            return
        # Der alte Token ist ab hier `401`. Erst danach faellt die Entscheidung,
        # ob die Datei mitkommt — nie umgekehrt.
        self._abort_principal(name)
        writer = self._writers.get(name)
        if writer is None:
            return
        try:
            writer(token)
        except Exception as exc:  # noqa: BLE001
            log.error("broker.credential_write_failed", principal=name,
                      kind=type(exc).__name__)

    def _abort_principal(self, name: str) -> None:
        for forward in list(self._inflight.get(name, ())):
            forward.abort()

    # ---- Datenebene ----------------------------------------------------

    async def _handle_models(self, request: Any) -> Any:
        """Tor 1 und Tor 3, dann **lokal** beantwortet."""
        from aiohttp import web

        moment = self._clock()
        principal = self._authenticate(request)
        if principal is None:
            return self._deny(request, None, "bad_token", 401, moment)
        try:
            self.registry.admit(principal, now=moment)
        except CapExceeded as exc:
            return self._deny(request, principal, exc.reason, 429, moment)

        payload = px.models_payload(now=moment,
                                    allowed=principal.caps.allowed_models)
        self._record(principal, request, outcome="answered_locally", status_code=200,
                     model="", moment=moment, path=px.MODELS_PATH)
        return web.json_response(payload)

    async def _handle_denied(self, request: Any) -> Any:
        """Alles, was nicht auf der Liste steht — lokal, ohne Anbieterkontakt.

        Hierher laufen auch die Abdrucksonden des angehefteten Hermes
        (`/api/tags`, `/v1/props`, `/version`, `/api/show`, `/v1/models/{name}`
        und die uebrigen). Sie bekommen eine kategorische Absage und erreichen
        nie das Netz.
        """
        moment = self._clock()
        principal = self._authenticate(request)
        if principal is None:
            return self._deny(request, None, "bad_token", 401, moment)
        return self._deny(request, principal, "path_not_allowed", 404, moment)

    async def _handle_forward(self, request: Any) -> Any:
        from aiohttp import web

        moment = self._clock()

        # Tor 1 — Token.
        principal = self._authenticate(request)
        if principal is None:
            return self._deny(request, None, "bad_token", 401, moment)

        # Tor 2 — Lease. `has_live_lease` bleibt die Frage, denn sie raeumt
        # dabei abgelaufene Leases weg; danach wird das Lease EINMAL aufgeloest
        # und durchgereicht. Budget, Duplikatgedaechtnis und Nachbuchung
        # muessen dasselbe Lease meinen wie die Buchzeile, sonst haette die
        # Aufgabengrenze eine zweite Wahrheit.
        #
        # Die beiden Aufrufe sind Absicht und keine Doppelarbeit: `live_lease`
        # allein raeumt NICHT auf, und dieses Aufraeumen gab es an dieser
        # Stelle schon vorher. Es hier wegzulassen waere eine stille
        # Verhaltensaenderung an einer Naht, die Zugang entscheidet.
        if not self.registry.has_live_lease(principal.name, now=moment):
            return self._deny(request, principal, "lease_absent", 403, moment)
        lease = self.registry.live_lease(principal.name, now=moment)
        if lease is None:                       # nur, wenn dazwischen etwas kippt
            return self._deny(request, principal, "lease_absent", 403, moment)

        # Tor 3 — Kappen gegen die bereits gebuchte Summe.
        try:
            self.registry.admit(principal, now=moment)
        except CapExceeded as exc:
            status = 429
            return self._deny(request, principal, exc.reason, status, moment)

        # Tor 4 — Pfad. Ein Wert, geprueft und weitergeleitet.
        path = px.canonical_path(request)
        if path not in px.FORWARDED_PATHS:
            return self._deny(request, principal, "path_not_allowed", 404, moment)

        # Rumpf puffern — die Kostenstelle, die ein echtes Modelltor verlangt.
        try:
            body_limit = (px.MAX_DOCUMENT_BODY_BYTES
                          if principal.name == DOCUMENT_PRINCIPAL else px.MAX_BODY_BYTES)
            raw = await self._read_body(request, max_bytes=body_limit)
        except px.BodyRejected as exc:
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path)

        digest = px.body_digest(raw)

        try:
            # Tor 5 — Modell. Geprueft gegen die Liste DIESES Auftraggebers,
            # nicht gegen die des Hauses: `gpt-5.4` ist bekannt, aber nur zwei
            # Auftraggeber duerfen es nennen. Der Grund bleibt `model_not_allowed`
            # — auf dem Draht ist „dieses Modell nicht" und „dieses Modell nicht
            # fuer dich" dieselbe Absage, und der Kaefig erfaehrt aus einer
            # Absage nichts ueber die Ordnung dahinter.
            model, body = px.parse_model(raw,
                                         allowed=principal.caps.allowed_models)
        except px.BodyRejected as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path, request_bytes=len(raw), lease=lease,
                              request_sha256=digest)

        # Tor 5b — anbieterseitige Werkzeuge. Geprueft gegen die Menge DIESES
        # Auftraggebers; Voreinstellung ist die LEERE Menge. `function` bleibt
        # immer erlaubt (klientenseitig). Ein neues anbieterseitiges Werkzeug
        # ist eine bewusste Politikaenderung je Auftraggeber, nie eine
        # Voreinstellung.
        try:
            px.check_provider_tools(body, allowed=principal.caps.allowed_provider_tools)
        except px.BodyRejected as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path, model=model, request_bytes=len(raw))

        # Eigenes Inhaltstor nur fuer die native Dokumentanalyse. Andere
        # Auftraggeber behalten exakt ihre bisherige Responses-Rumpfform. Vor
        # dem Doppelgaenger-Tor, weil eine abgelehnte Form gar nicht erst
        # gehasht und gemerkt werden soll.
        if principal.name == DOCUMENT_PRINCIPAL:
            try:
                px.check_input_shape(body, allowed_mime=ALLOWED_DOCUMENT_MIME)
            except px.BodyRejected as exc:
                self._release_buffer(len(raw))
                return self._deny(request, principal, exc.reason, exc.status, moment,
                                  path=path, model=model, request_bytes=len(raw))

        # Tor 6 — der versehentliche Doppelgaenger. VOR der Vorbelastung, damit
        # er nichts kostet: ein Duplikat darf weder Token noch einen Anruf des
        # Aufgabenbudgets verbrauchen.
        #
        # Was hier NICHT hineinfaellt und ausdruecklich weiterlaeuft: der
        # naechste Rechercheschritt (der Rumpf waechst, also ein anderer Hash),
        # die Wiederholung nach einem Transport- oder Anbieterfehler (der
        # Vorgaenger hat nichts geliefert, also steht sein Hash nicht im
        # Gedaechtnis), eine neue Aufgabe (eigenes Lease) und jede
        # Statusabfrage (beruehrt den Broker gar nicht).
        if lease.already_forwarded(digest, moment):
            self._release_buffer(len(raw))
            return self._deny(request, principal, "duplicate_request", 409, moment,
                              path=path, model=model, request_bytes=len(raw),
                              lease=lease, request_sha256=digest)

        # Tor 7 — Aufgabenbudget, dann Tageskappe, dann gebucht. Reisst eines
        # von beiden, geht nichts raus.
        estimate = px.estimate_tokens(raw, body, model=model)
        try:
            self.registry.precharge(principal, estimate=estimate, now=moment,
                                    lease=lease)
        except CapExceeded as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, 429, moment,
                              path=path, model=model, request_bytes=len(raw),
                              lease=lease, request_sha256=digest)

        # Tor 8 — weiterleiten.
        try:
            return await self._forward(request, principal, path=path,
                                       model=model, raw=raw, estimate=estimate,
                                       moment=moment, lease=lease, digest=digest)
        finally:
            self._release_buffer(len(raw))

    async def _handle_anthropic(self, request: Any) -> Any:
        """Die Anthropic-Flaeche. Dieselben Tore, ein eigenes Ziel.

        Zwei Unterschiede zur OpenAI-Flaeche, beide bewusst:

        1. **Ein zusaetzliches Tor:** nur die zwei Schreiber-Auftraggeber
           duerfen hier ueberhaupt etwas. Ein Deep-, Bot- oder Lead-Token ist
           hier eine Absage — nicht, weil es kein Modell traefe, sondern weil
           es auf dieser Flaeche nichts zu suchen hat.
        2. **Die Abfragezeichenkette wird verworfen statt abgelehnt.** Die
           gemessene CLI ruft `/v1/messages?beta=true`. Weitergereicht wird sie
           nicht (das waere anfragegesteuerter Inhalt beim Anbieter), abgelehnt
           auch nicht (das waere ein Broker, der den einzigen realen Klienten
           nicht bedient). Braucht der Anbieter sie, wird sie eine gepinnte
           Konstante in `anthropic.py`.
        """
        moment = self._clock()

        principal = self._authenticate(request)
        if principal is None:
            return self._deny(request, None, "bad_token", 401, moment)

        # Tor 1b — geschlossene Auftraggebermenge dieser Flaeche.
        if principal.name not in ANTHROPIC_PRINCIPALS:
            return self._deny(request, principal, "principal_not_allowed", 403,
                              moment, path=request.path)

        if not self.registry.has_live_lease(principal.name, now=moment):
            return self._deny(request, principal, "lease_absent", 403, moment)
        # Dasselbe Lease wie auf der OpenAI-Flaeche EINMAL aufgeloest und
        # durchgereicht: nur so traegt auch die WEITERGELEITETE Zeile den
        # Auftragsverweis (`task_ref`), aus dem der Nexus-Auftragsweg den
        # Verbrauch und den bewiesenen Nichtstart je Auftrag liest.
        lease = self.registry.live_lease(principal.name, now=moment)
        if lease is None:                       # nur, wenn dazwischen etwas kippt
            return self._deny(request, principal, "lease_absent", 403, moment)

        try:
            self.registry.admit(principal, now=moment)
        except CapExceeded as exc:
            return self._deny(request, principal, exc.reason, 429, moment, lease=lease)

        path = str(request.path)
        if path not in an.FORWARDED_PATHS:
            return self._deny(request, principal, "path_not_allowed", 404, moment, lease=lease)

        # Tor 3b — Anmeldung ueberhaupt vorhanden? Ohne sie geht nichts raus,
        # und der Grund ist benannt: der Autopilot liest ihn als
        # `no_credential` und meldet den Schreiber UNAVAILABLE.
        if not self.anthropic.configured():
            return self._deny(request, principal, "no_credential", 503, moment,
                              path=path, lease=lease)

        try:
            raw = await self._read_body(request)
        except px.BodyRejected as exc:
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path, lease=lease)

        try:
            # Die Schnittmenge: was diese Flaeche kennt UND was dieser
            # Auftraggeber nennen darf. Ein OpenAI-Modellname faellt hier,
            # auch wenn er anderswo im Haus gueltig ist.
            erlaubt = principal.caps.allowed_models & an.MODEL_ALLOWLIST
            model, _body = px.parse_model(raw, allowed=erlaubt)
        except px.BodyRejected as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path, request_bytes=len(raw), lease=lease)

        # Tor 5b, Anthropic-Fassung — VOR dem Vorabbuchen (Review Runde 16, B16-1).
        try:
            px.check_anthropic_provider_tools(_body, allowed=principal.caps.allowed_provider_tools)
        except px.BodyRejected as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, exc.status, moment,
                              path=path, model=model, request_bytes=len(raw), lease=lease)

        estimate = px.estimate_tokens(raw, _body, model=model)
        try:
            self.registry.precharge(principal, estimate=estimate, now=moment,
                                    lease=lease)
        except CapExceeded as exc:
            self._release_buffer(len(raw))
            return self._deny(request, principal, exc.reason, 429, moment,
                              path=path, model=model, request_bytes=len(raw),
                              lease=lease)

        try:
            return await self._forward(request, principal, path=path,
                                       model=model, raw=raw, estimate=estimate,
                                       moment=moment, lease=lease,
                                       upstream=self.anthropic,
                                       url=an.target_url(path))
        except an.AnthropicAuthError as exc:
            # Der Wert war da, ist es aber nicht mehr (gesperrt, widerrufen,
            # unlesbar). Das ist eine Lage, kein Absturz — und der Grund reist
            # ins Buch, nicht der Wert.
            log.error("broker.anthropic_auth_failed", reason=exc.reason)
            return self._deny(request, principal, exc.reason, 503, moment,
                              path=path, model=model, request_bytes=len(raw),
                              lease=lease)
        finally:
            self._release_buffer(len(raw))

    async def _forward(self, request: Any, principal: Any, *, path: str,
                       model: str, raw: bytes, estimate: int,
                       moment: float, lease: Any = None,
                       digest: str = "", upstream: Any = None,
                       url: str = "") -> Any:
        """Der eine Weiterleitungsweg — fuer beide Flaechen.

        `upstream` und `url` sind Parameter und keine Attribute, weil es zwei
        Flaechen gibt und jede ihr eigenes gepinntes Ziel und ihren eigenen
        Kopfsatzbau hat. Was sie teilen, ist alles danach: Buchung, Strom,
        Nutzungsmessung, Abbruch. Zwei Kopien davon waeren die erste, die
        jemand zu pflegen vergisst.

        `lease` und `digest` kommen vom Deep-Strang: sie tragen die
        Aufgabengrenzen und den Duplikatschutz. Beides gilt fuer beide
        Flaechen — eine Grenze, die nur fuer einen Anbieter zaehlt, waere
        keine.
        """
        upstream = upstream if upstream is not None else self.upstream
        from aiohttp import web

        lease_id = lease.lease_id if lease is not None else ""
        task_ref = lease.ref if lease is not None else ""
        # Der Kopfsatz entsteht VOR der Buchzeile: die Anthropic-Flaeche leiht
        # dabei aus dem Tresor, und eine Absage des Tresors
        # (`AnthropicAuthError`) ist eine ABLEHNUNG des Aufrufers, keine
        # Weiterleitung, die am Anbieter scheiterte. Stuende der Aufruf im
        # Weiterleitungsblock unten, fiele er in dessen generischen Fang und
        # wuerde als `upstream_error`/502 gebucht — genau so war es bis N8/C4,
        # und der `except AnthropicAuthError` des Aufrufers war unerreichbar.
        headers = (upstream.outbound_headers(request, principal=principal.name)
                   if upstream is self.anthropic
                   else upstream.outbound_headers(request))
        row = self.ledger.record(px.ledger_entry(
            principal=principal.name, generation=principal.generation,
            method=request.method, path=path, outcome="forwarded",
            lease_id=lease_id, task_ref=task_ref, model=model,
            request_bytes=len(raw), input_tokens=estimate, output_tokens=0,
            tokens_source="estimated", request_sha256=digest), at=moment)

        forward = px.Forward(principal=principal.name)
        self._inflight.setdefault(principal.name, set()).add(forward)
        principal.inflight += 1

        sniffer = px.UsageSniffer()
        started = time.monotonic()
        sent = 0
        status = 502
        outcome = "upstream_error"
        #: Ist der Strom bis zum Ende durchgelaufen? Nur dann gilt der Rumpf
        #: als GELIEFERT — und nur dann merkt sich das Lease seinen Hash.
        #: Ein abgerissener oder abgebrochener Strom hat dem Kaefig keine
        #: vollstaendige Antwort gegeben; seine Wiederholung ist genau der
        #: Transport-Retry, den Hermes intern einmal macht und der bleiben
        #: soll. Ihn zu sperren waere ein Rueckschritt, kein Schutz.
        interrupted = False
        out: Any = None
        session = upstream.session()
        try:
            async with session.post(url or px.upstream_url(path), data=raw,
                                    headers=headers,
                                    allow_redirects=False) as response:
                status = response.status
                if 300 <= status < 400:
                    # Ein gefolgter Umzug truege den echten Schluessel mit.
                    # Kategorisch, ohne Ausnahme, ohne Ziel im Rumpf.
                    log.error("broker.upstream_redirect", status=status)
                    return self._categorical(502, "upstream_redirect_refused")
                forward.response = response
                forward.on_abort(response.close)

                out = web.StreamResponse(
                    status=status, headers=up.safe_response_headers(response.headers))
                await out.prepare(request)
                try:
                    async for chunk in response.content.iter_any():
                        if forward.aborted or not self._listening:
                            interrupted = True
                            break
                        await out.write(chunk)
                        sent += len(chunk)
                        sniffer.feed(chunk)
                except Exception as exc:  # noqa: BLE001
                    interrupted = True
                    # Ab hier ist der Kopfsatz beim Kaefig. Ein Abbruch — auch
                    # der eigene bei Lease-Null — beendet den Strom, er ersetzt
                    # ihn NICHT durch eine neue Antwort: nach `prepare` gibt es
                    # keine zweite Antwort mehr, und der Versuch liesse den
                    # Aufrufer haengen statt ihn abzuweisen.
                    log.info("broker.stream_ended", kind=type(exc).__name__,
                             aborted=forward.aborted)
                sniffer.finish()
                await out.write_eof()
                if forward.aborted and status < 400:
                    # Die Schleife endete ueber den Abbruch-Ast oben (Zeile
                    # 540), nicht ueber eine Ausnahme — der Anbieter hat mit
                    # einem Erfolgscode geantwortet, der Kaefig hat den Strom
                    # zugemacht. `forwarded` waere hier falsch, weil der Strom
                    # nicht vollstaendig weitergereicht wurde; `upstream_error`
                    # waere die falsche Aussage, die dieser Fund beschreibt.
                    outcome = "client_aborted"
                else:
                    outcome = "forwarded" if status < 400 else "upstream_error"
                return out
        except asyncio.CancelledError:
            outcome = "client_aborted" if forward.aborted and status < 400 else "upstream_error"
            raise
        except Exception as exc:  # noqa: BLE001
            # Der rohe Fehlertext des Anbieters wird NICHT ins Buch kopiert.
            log.error("broker.upstream_failed", kind=type(exc).__name__)
            if forward.aborted and status < 400:
                # Der Anbieter hat mit einem Erfolgscode geantwortet — der
                # Strom endete, weil der Klient abbrach, nicht weil der
                # Anbieter scheiterte. `upstream_error` waere hier eine
                # falsche Aussage.
                outcome = "client_aborted"
            else:
                outcome = "upstream_error"
            if out is not None:
                # Schon gesendet: dann sauber beenden statt eine zweite Antwort
                # zu versuchen.
                try:
                    await out.write_eof()
                except Exception:  # noqa: BLE001
                    pass
                return out
            return self._categorical(502, "upstream_unavailable")
        finally:
            await _close_quietly(session)
            principal.inflight = max(0, principal.inflight - 1)
            self._inflight.get(principal.name, set()).discard(forward)
            # EINE Entscheidung, EINE Stelle — und sie korrigiert die Kappe
            # auch dann, wenn `usage` ausblieb, der Verbrauch aber messbar
            # war (DEBT-0185).
            ein, aus, quelle = px.settled_charge(
                seen=sniffer.seen, input_tokens=sniffer.input_tokens,
                output_tokens=sniffer.output_tokens, estimate=estimate,
                request_bytes=len(raw), sent_bytes=sent,
                aborted=forward.aborted, status=status)
            if quelle != px.TOKEN_SOURCE_ESTIMATED:
                # Ersetzt die Vorbelastung — nicht additiv, sonst waere jede
                # Anfrage doppelt gebucht.
                self.registry.replace_estimate(principal, estimate=estimate,
                                               reported=ein + aus, now=moment,
                                               lease=lease)
            # Gemerkt wird NUR ein Rumpf, der tatsaechlich geliefert wurde.
            # `outcome == "forwarded"` heisst hier schon `status < 400` (siehe
            # oben) — ein abgelehnter oder abgerissener Versuch hat nichts
            # geliefert, und seine Wiederholung ist deshalb legitim.
            if lease is not None and outcome == "forwarded" and not interrupted:
                lease.note_forwarded(digest, moment)
            self.ledger.settle(
                row, input_tokens=ein, output_tokens=aus,
                response_bytes=sent, status_code=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome, tokens_source=quelle)

    # ---- Innenleben ----------------------------------------------------

    def _authenticate(self, request: Any) -> Any:
        """Der Broker-Token aus der Anfrage — aus BEIDEN Koepfen, die ein
        Klient dafuer benutzt.

        `Authorization: Bearer` ist die OpenAI-Form. Die Anthropic-Form ist
        `x-api-key`, und die schickt Claude Code, wenn die Anmeldung aus
        `ANTHROPIC_API_KEY` kommt — live gemessen am 2026-09-02: der erste
        Live-Beweis endete an drei `bad_token`-Zeilen, weil hier nur der
        Bearer gelesen wurde.

        Das ist keine zweite Berechtigung, sondern derselbe Wert an der
        Stelle, an die ihn das jeweilige Protokoll schreibt. Was er oeffnet,
        entscheiden unveraendert die Tore danach: Auftraggeber, Lease, Pfad,
        Modell, Kappe.
        """
        header = str(request.headers.get("Authorization", "") or "")
        if header.lower().startswith("bearer "):
            return self.registry.resolve(header[7:].strip())
        api_key = str(request.headers.get("x-api-key", "") or "").strip()
        if api_key:
            return self.registry.resolve(api_key)
        return None

    async def _read_body(self, request: Any, *, max_bytes: int = px.MAX_BODY_BYTES) -> bytes:
        """Vollstaendig puffern — bis zur Obergrenze, einzeln und in Summe."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await request.content.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                self._release_buffer(total - len(chunk))
                raise px.BodyRejected("body_too_large", status=413)
            if self._buffered + total > px.MAX_TOTAL_BUFFERED_BYTES:
                self._release_buffer(total - len(chunk))
                raise px.BodyRejected("body_too_large", status=413)
            chunks.append(chunk)
        self._buffered += total
        return b"".join(chunks)

    def _release_buffer(self, size: int) -> None:
        self._buffered = max(0, self._buffered - size)

    def _deny(self, request: Any, principal: Any, reason: str, status: int,
              moment: float, *, path: str = "", model: str = "",
              request_bytes: int = 0, lease: Any = None,
              request_sha256: str = "") -> Any:
        name = principal.name if principal is not None else "unknown"
        generation = principal.generation if principal is not None else 0
        lease_id, task_ref = ("", "")
        if lease is not None:
            lease_id, task_ref = lease.lease_id, lease.ref
        elif principal is not None:
            lease_id, task_ref = self.registry.lease_ref(name, now=moment)
        self.ledger.record(px.ledger_entry(
            principal=name, generation=generation, method=request.method,
            path=path or px.canonical_path(request) or request.path,
            outcome="denied", lease_id=lease_id, task_ref=task_ref, model=model,
            status_code=status, request_bytes=request_bytes,
            denied_reason=reason, request_sha256=request_sha256), at=moment)
        log.info("broker.denied", principal=name, reason=reason, status=status)
        return self._categorical(status, reason, retry_after=self._retry_after(reason, moment))

    @staticmethod
    def _retry_after(reason: str, moment: float) -> int:
        """Wie lange Warten hier wirklich hilft — oder null, wenn es das nicht tut.

        Eine Zahl steht nur da, wo sie aus der Wahrheit des Brokers folgt. Ein
        `Retry-After` auf einer Ablehnung, die sich nie erholt, waere eine
        Einladung zu genau dem Retry-Sturm, den dieser Milestone abstellt.
        """
        rule = _RETRY_AFTER.get(reason)
        return int(rule(moment)) if rule is not None else 0

    def _record(self, principal: Any, request: Any, *, outcome: str,
                status_code: int, model: str, moment: float, path: str) -> None:
        lease_id, task_ref = self.registry.lease_ref(principal.name, now=moment)
        self.ledger.record(px.ledger_entry(
            principal=principal.name, generation=principal.generation,
            method=request.method, path=path, outcome=outcome,
            lease_id=lease_id, task_ref=task_ref, model=model,
            status_code=status_code), at=moment)

    @staticmethod
    def _categorical(status: int, reason: str, *, retry_after: int = 0) -> Any:
        """Eine Absage ohne Innenleben. Kein Anbietertext, keine Diagnose.

        `Retry-After` ist die einzige Zahl, die mitgeht — und nur, wenn sie
        stimmt. Sie ist Metadatum fuer einen wohlerzogenen Client, keine
        Diagnose: sie sagt WANN wieder, nie WARUM nicht.
        """
        from aiohttp import web

        headers = {"Retry-After": str(int(retry_after))} if retry_after > 0 else None
        return web.json_response({"error": {"type": "solvio_broker",
                                            "code": reason}}, status=status,
                                 headers=headers)


async def _close_quietly(session: Any) -> None:
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        pass


def _utc_midnight(moment: float) -> float:
    """Der Beginn des laufenden UTC-Tages — dieselbe Grenze wie die Kappe.

    Hier stand `time.mktime(time.strptime(utc_day(moment))) - time.timezone`,
    und das war in einer Sommerzeitzone um eine Stunde daneben (gemessen am
    2026-08-29 in Europe/Berlin: 23:00 des Vortags). Die Folge war keine
    Kappenumgehung, sondern eine falsche Fenstergrenze beim Nachsaeen des
    Tagesverbrauchs nach einem Core-Neustart. Die Rechnung liegt jetzt an genau
    einer Stelle, in `session.py`, neben `utc_day`.
    """
    return utc_midnight(moment)
