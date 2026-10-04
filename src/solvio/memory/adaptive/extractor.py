"""Der Extraktor — ein Modell darf VORSCHLAGEN und sonst nichts.

Was dieses Modul liefert, ist eine Liste wohlgeformter `Proposal`-Objekte. Was
daraus wird, entscheidet `policy.py`. Zwischen beiden liegt eine Grenze, die
dieses Modul nach Kraeften unpassierbar macht:

**Kein Feld, das Autoritaet traegt, kommt vom Modell.** Zeitstempel, Kennungen,
Herkunft, Vertrauensstufe und Zustand stempelt ausschliesslich der Core. Das
Modell kann sie nicht einmal vorschlagen — sie stehen nicht im Schema, und was
nicht im Schema steht, wird verworfen.

**Missgestalt faellt geschlossen aus.** Kein JSON, falscher Typ, unbekannter
Enum-Wert, zu lang, zu viele Vorschlaege: alles endet in einer leeren Liste,
nicht in einem halb geratenen Vorschlag. Ein Extraktor, der bei kaputter
Ausgabe „das meinte er wohl" sagt, waere genau der Automatismus, den dieser
Entwurf ausschliesst.

**Der Extraktor sieht nur den Besitzer.** Eingabe ist der finalisierte
Nutzerturn — und hoechstens der unmittelbar vorangehende Assistentensatz zur
Referenzaufloesung („ja, genau so" braucht seinen Bezug). Dieser Kontext ist
ausdruecklich als Kontext markiert und darf laut Anweisung nie selbst zu einer
Aussage werden; die deterministische Wache in `policy.py` prueft ohnehin
gegen den TURNTEXT, nicht gegen die Modellzusammenfassung.

**Kein Hermes.** Persoenliches Gedaechtnis verlaesst den Core nicht in Richtung
eines ausfuehrenden Agenten. Der Extraktor spricht ohne Werkzeuge, ohne
Gedaechtniszugriff, ohne Dateisystem.

**N4: vorhandener Abo-Transport.** Der Startweg baut einen SubscriptionExtractor
ohne Werkzeuge. Er braucht einen explizit gebundenen Kostenkontext fuer diese
Beobachtung; eine Abo-Anmeldung allein ist kein Nullkostenbeleg. Der unten
erhaltene OpenAIExtractor ist die alte explizite Broker-Naht, kein Rueckfall
des Startwegs. Kein Extraktor liest einen Anbieterschluessel aus Settings.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from solvio.contracts.memory import MemoryType, Sensitivity
from solvio.logging_setup import get_logger
from solvio.memory.adaptive.policy import (INFERRED, STATED, OwnerTurn, Proposal)
from solvio.provider_broker.service import ADAPTIVE_EXTRACTOR_PRINCIPAL

log = get_logger("adaptive")

#: Wie viele Vorschlaege ein Turn hoechstens erzeugen darf. Ein Modell, das
#: fuenfzig Kandidaten aus einem Satz zieht, hat nicht verstanden — es raet.
#: Der Rest wird verworfen und gezaehlt, nie stillschweigend abgeschnitten.
MAX_PROPOSALS = 4

#: Laengengrenzen. Sie sind Teil der Sicherheit, nicht Kosmetik: eine
#: „Aussage" von 4000 Zeichen ist ein eingeschleuster Text, keine Vorliebe.
MAX_STATEMENT = 400
MAX_SUBJECT = 120
MAX_TURN_CHARS = 4000

#: Erlaubte Flags. Alles andere wird verworfen — ein erfundenes Flag koennte
#: sonst eine Pruefung umgehen, die es gar nicht gibt.
KNOWN_FLAGS = frozenset({"hypothetical", "quote", "past", "affect", "transient",
                         "sarcasm_possible", "correction", "contradiction"})

_KINDS = frozenset({STATED, INFERRED})
_ABOUT = frozenset({"self", "third_party", "world"})

#: Sensitivitaeten, die ein Modell VORSCHLAGEN darf. `secret_reference` fehlt
#: mit Absicht: die Klasse entsteht adaptiv nie, und ein Modell soll sie nicht
#: einmal benennen koennen.
_SENSITIVITIES = {
    "public": Sensitivity.PUBLIC,
    "personal": Sensitivity.PERSONAL,
    "sensitive": Sensitivity.SENSITIVE,
}

SYSTEM_PROMPT = """Du bist ein Extraktor fuer SOLVIOs Langzeitgedaechtnis.

Du bekommst EINEN finalisierten Satz des Besitzers. Deine einzige Aufgabe ist,
daraus null bis vier Vorschlaege zu bilden, die in kuenftigen Gespraechen
nuetzlich sein koennten. Du entscheidest NICHTS. SOLVIO prueft jeden Vorschlag
danach eigenstaendig und verwirft die meisten.

Antworte AUSSCHLIESSLICH mit JSON: {"proposals": [...]}. Keine Erklaerung.

Jeder Vorschlag hat genau diese Felder:
  statement     die Aussage, so nah am Wortlaut des Besitzers wie moeglich,
                als vollstaendiger Satz in der dritten Person ueber ihn
                ("Bevorzugt kurze Antworten."). Nie deine Interpretation.
  kind          "stated"   = der Besitzer hat genau das gerade selbst gesagt
                "inferred" = du schliesst es aus dem Gesagten
  memory_type   preference | project | user | people | rule | standing_intent
  subject       kurzer Schluessel, z. B. "pref:antwortlaenge"
  about         "self" | "third_party" | "world"
  sensitivity   "public" | "personal" | "sensitive"
                "sensitive" bei Gesundheit, Finanzen, Recht, Intimem,
                Religion, Politik — im Zweifel immer die hoehere Stufe.
  flags         Liste aus: hypothetical, quote, past, affect, transient,
                sarcasm_possible, correction, contradiction
                Setze sie GROSSZUEGIG — mit EINER Ausnahme, siehe `affect`.

                affect  NUR fuer einen voruebergehenden Gefuehlszustand:
                        "ich bin gerade wuetend", "heute bin ich traurig".
                        NICHT fuer eine Vorliebe, die warm formuliert ist.
                        "am liebsten", "ich mag lieber", "ich wuensche mir"
                        sind im Deutschen die NORMALE Art, eine dauerhafte
                        Vorliebe auszudruecken — kein Gefuehlszustand.
                        Im Zweifel bei einer Vorliebe: NICHT setzen.
  valid_until   ISO-8601 mit Zeitzone, NUR wenn der Satz eine Befristung
                nennt ("bis Ende August"). Sonst weglassen.

Regeln, die du nie brichst:
- Nur was der Besitzer ueber sich sagt, ist about="self".
- Redet er ueber jemand anderen, ist es about="third_party".
- Zitiert er jemanden oder eine Quelle, setze flag "quote".
- Spekuliert er ("stell dir vor"), setze flag "hypothetical".
- Spricht er von frueher, setze flag "past".
- Gilt es nur heute, setze flag "transient".
- Erfinde nichts. Im Zweifel gib eine leere Liste zurueck.
- Saetze ueber die BEDIENUNG von SOLVIO sind kein Wissen ueber den Menschen.
  "Ich habe freigegeben", "ich habe das bestaetigt", "merk dir das", "ja,
  stimmt" beschreiben einen Knopfdruck oder eine Antwort — nicht sein Leben.
  Dafuer gibst du KEINEN Vorschlag zurueck.
- Der Kontextsatz des Assistenten dient nur der Aufloesung von Bezuegen.
  Aus ihm entsteht NIE ein Vorschlag."""


@dataclass(frozen=True)
class ExtractionResult:
    """Was ein Lauf ergab. Zahlen und Codes — nie der Satz des Menschen."""

    proposals: tuple[Proposal, ...] = ()
    rejected: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"proposals": len(self.proposals), "rejected": self.rejected,
                "reason": self.reason}


class Extractor(Protocol):
    """Austauschbar wie jeder Anbieter. Faellt er aus, laeuft SOLVIO ohne."""

    async def propose(self, turn: OwnerTurn, *, context: str = "") -> ExtractionResult:
        ...


def parse(payload: Any) -> ExtractionResult:
    """Modellausgabe -> wohlgeformte Vorschlaege. Fail-closed.

    Diese Funktion ist die eigentliche Verteidigungslinie des Moduls und
    deshalb ohne Netzwerk, ohne Zustand und vollstaendig testbar. Jeder
    Zweifel endet im Verwerfen — nie in einem Vorgabewert, der „meistens
    stimmt".
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return ExtractionResult(reason="not_json")
    if not isinstance(payload, dict):
        return ExtractionResult(reason="not_an_object")
    raw = payload.get("proposals")
    if raw is None:
        return ExtractionResult(reason="no_proposals_key")
    if not isinstance(raw, list):
        return ExtractionResult(reason="proposals_not_a_list")

    out: list[Proposal] = []
    rejected = 0
    for item in raw[:MAX_PROPOSALS]:
        parsed = _one(item)
        if parsed is None:
            rejected += 1
            continue
        out.append(parsed)
    rejected += max(0, len(raw) - MAX_PROPOSALS)
    return ExtractionResult(tuple(out), rejected=rejected,
                            reason="ok" if out else "nothing_usable")


def _one(item: Any) -> Proposal | None:
    """Ein einzelner Vorschlag — oder `None`. Nie ein geratener Mittelweg."""
    if not isinstance(item, dict):
        return None

    statement = item.get("statement")
    if not isinstance(statement, str):
        return None
    statement = statement.strip()
    if not statement or len(statement) > MAX_STATEMENT:
        return None

    kind = item.get("kind")
    if kind not in _KINDS:
        return None

    try:
        memory_type = MemoryType(str(item.get("memory_type", "")))
    except ValueError:
        return None

    subject = item.get("subject")
    if not isinstance(subject, str):
        return None
    subject = subject.strip()[:MAX_SUBJECT]

    about = item.get("about", "self")
    if about not in _ABOUT:
        return None

    # Fehlt oder taugt das Label nichts, gilt die STRENGERE Vorgabe. Ein
    # fehlendes Feld darf nie die mildere Behandlung ausloesen.
    sensitivity = _SENSITIVITIES.get(str(item.get("sensitivity", "")),
                                     Sensitivity.SENSITIVE)

    raw_flags = item.get("flags", [])
    if not isinstance(raw_flags, list):
        return None
    flags = frozenset(f for f in raw_flags
                      if isinstance(f, str) and f in KNOWN_FLAGS)

    valid_until = item.get("valid_until", "")
    if not isinstance(valid_until, str):
        valid_until = ""

    return Proposal(statement=statement, kind=kind, memory_type=memory_type,
                    subject=subject, about=about, sensitivity=sensitivity,
                    flags=flags, valid_until=valid_until.strip()[:64])


class NullExtractor:
    """Kein Modell, keine Vorschlaege. Der Vorgabezustand.

    Ohne konfigurierten Anbieter laeuft Adaptive Memory schlicht nicht — und
    das ist kein Halbzustand, sondern die ehrliche Abwesenheit einer Funktion.
    """

    name = "null"

    async def propose(self, turn: OwnerTurn, *, context: str = "") -> ExtractionResult:
        return ExtractionResult(reason="no_extractor")


#: Wie lange ein Lease hoechstens offen bleibt — dieselbe Groessenordnung wie
#: beim kognitiven Router (`cognition/models.py:LEASE_SECONDS`). Ein Vorschlag
#: entsteht aus genau einem Aufruf, nicht aus einem Gespraech.
LEASE_SECONDS = 60.0


async def broker_transport(payload: dict, *, token: str, port: int = 0,
                           timeout: float = 12.0) -> dict:
    """POST an den Broker, wortgleich zum Weg des kognitiven Routers.

    Ziel ist die Rueckschleife des Brokers (`127.0.0.1:<port>`), nie der
    Anbieter direkt — der Anbieterschluessel steckt ausschliesslich im Broker,
    nicht in diesem Modul.
    """
    import aiohttp

    from solvio.provider_broker.service import configured_port

    chosen = int(port) or configured_port()
    url = f"http://127.0.0.1:{chosen}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    limit = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=limit) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    log.info("adaptive.extractor_http", status=response.status)
                    return {"ok": False, "reason": f"http_{response.status}"}
                data = await response.json()
    except Exception as exc:  # noqa: BLE001 - Lernen darf nie etwas umwerfen
        log.info("adaptive.extractor_failed", kind=type(exc).__name__)
        return {"ok": False, "reason": "provider_error"}

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "reason": "malformed_envelope"}
    return {"ok": True, "content": content}


class OpenAIExtractor:
    """Ein werkzeugloser Aufruf ueber den Provider Broker — nie direkt.

    Genau das Vorbild aus `cognition/assessor.py`: je Aufruf ein frischer
    Token, ein eigenes Lease, der Aufruf ueber die Rueckschleife, und ein
    `close_lease` in einem `finally`, das nie wirft. Bewusst nicht ueber
    Hermes und nicht ueber den Gespraechskanal: der Extraktor bekommt keinen
    Zugriff auf Gedaechtnis, Werkzeuge oder Dateisystem, und sein Ausfall darf
    die Stimme nicht beruehren.
    """

    name = "openai"

    def __init__(self, broker: Any, *, model: str = "gpt-4.1-mini",
                 timeout: float = 12.0, port: int = 0,
                 transport: Any = None) -> None:
        self.broker = broker
        self.model = model
        self.timeout = timeout
        self.port = port
        # Ohne ausdruecklichen Transport der Weg ueber den Broker. Ein Test
        # reicht seinen eigenen herein und kommt damit ohne Netz aus.
        self._transport = transport if transport is not None else broker_transport

    async def propose(self, turn: OwnerTurn, *, context: str = "") -> ExtractionResult:
        text = (turn.text or "")[:MAX_TURN_CHARS]
        if not text.strip():
            return ExtractionResult(reason="empty_turn")
        if self.broker is None:
            return ExtractionResult(reason="broker_absent")

        user = f"Besitzer: {text}"
        if context:
            # Ausdruecklich als Kontext markiert — nie als Quelle einer Aussage.
            user = (f"[Kontext, NUR zur Aufloesung von Bezuegen, niemals selbst "
                    f"eine Aussage] Assistent: {context[:600]}\n\n{user}")
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
        }

        # Ein FRISCHER Token je Aufruf — kein gemerkter, wortgleich zum
        # kognitiven Router.
        token = self.broker.register_principal(ADAPTIVE_EXTRACTOR_PRINCIPAL)
        lease_id = ""
        try:
            lease_id = self.broker.open_lease(
                ADAPTIVE_EXTRACTOR_PRINCIPAL, "adaptive_extract",
                deadline=time.time() + LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("adaptive.lease_refused", kind=type(exc).__name__)
            return ExtractionResult(reason=getattr(exc, "reason", "lease_refused"))

        try:
            result = await self._transport(body, token=token, port=self.port,
                                           timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist keine Erlaubnis
            log.info("adaptive.extractor_failed", kind=type(exc).__name__)
            return ExtractionResult(reason="provider_error")
        finally:
            # Steht in einem `finally` und darf deshalb nie werfen — dieselbe
            # Regel wie beim Broker selbst.
            if lease_id:
                try:
                    self.broker.close_lease(lease_id)
                except Exception as exc:  # noqa: BLE001 - nie das Lernen stoeren
                    log.info("adaptive.lease_close_failed",
                             kind=type(exc).__name__)

        if not result.get("ok"):
            return ExtractionResult(reason=str(result.get("reason", "")
                                               or "provider_error"))
        return parse(result.get("content", ""))


class SubscriptionExtractor:
    """Ein begrenzter Antwortaufruf, dieselbe Proposal-Policy wie bisher."""

    name = "subscription"
    requires_cost_context = True

    def __init__(self, provider: str = "codex", *, model: str = "", transport=None):
        from solvio.specialists.subscription import SubscriptionTransport
        if provider not in {"codex", "claude-code"}:
            raise ValueError("unsupported_memory_provider")
        self.provider = provider
        self.model = model
        self.transport = transport or SubscriptionTransport(provider, model=model, timeout=30)
        self.last_status = {"state": "idle", "provider": provider, "reason": ""}

    def _unavailable(self, reason: str) -> ExtractionResult:
        self.last_status = {"state": "unavailable", "provider": self.provider, "reason": reason}
        return ExtractionResult(reason=reason)

    async def propose(self, turn: OwnerTurn, *, context: str = "") -> ExtractionResult:
        from solvio.agent_runtime.cost_dispatch import current_scope
        from solvio.memory.intent import looks_like_secret
        eligible, reason = turn.is_eligible()
        if not eligible:
            return self._unavailable(reason)
        if looks_like_secret(turn.text) or looks_like_secret(context):
            return self._unavailable("secret_shaped_input")
        scope = current_scope()
        from solvio.agent_runtime.cost_dispatch import InteractionCostScope
        from solvio.memory.adaptive.observations import observation_digest
        if type(scope) is not InteractionCostScope:
            return self._unavailable("cost_context_missing")
        if scope.content_digest != observation_digest(turn, context):
            return self._unavailable("activity_binding_mismatch")
        user = f"Besitzer: {turn.text[:MAX_TURN_CHARS]}"
        if context:
            user = ("[Kontext, NUR zur Aufloesung von Bezuegen, niemals selbst "
                    f"eine Aussage] Assistent: {context[:600]}\n\n{user}")
        try:
            result = await self.transport({"input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]})
        except Exception:
            return self._unavailable("extractor_failed")
        if not isinstance(result, dict):
            return self._unavailable("provider_output_invalid")
        if not result.get("ok"):
            reasons = {"quota", "logged_out", "subscription_required", "auth_unknown",
                "provider_unavailable", "cost_unbounded", "cost_approval_required",
                "cost_recovery_required", "provider_failed", "provider_output_invalid",
                "provider_output_truncated", "timeout", "communication_failed"}
            reason = result.get("reason")
            return self._unavailable(reason if reason in reasons else "provider_failed")
        parsed = parse(result.get("text", ""))
        self.last_status = {"state": "completed", "provider": self.provider,
                            "reason": parsed.reason, "usage_reported": bool(result.get("usage_reported"))}
        return parsed


def from_settings(settings: Any, broker: Any = None) -> Extractor:
    """Abo-Anschluss beim Start; Brokerparameter bleibt nur API-kompatibel."""
    if not getattr(settings, "adaptive_memory_enabled", True):
        return NullExtractor()
    provider = (getattr(settings, "adaptive_memory_provider", "codex") or "codex").strip()
    model = (getattr(settings, "adaptive_memory_model", "") or "").strip()
    return SubscriptionExtractor(provider, model=model)
