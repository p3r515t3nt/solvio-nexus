"""Das **einzige** Modul, das die Anthropic-Anmeldung beruehrt.

Die Bauform ist die des Offsite-Transports (`storage/offsite/s3.py`) und der
Zahlung (`payment/executor.py`): ein Wert erreicht genau ein namentlich
geprueftes Modul, und die Bindung dorthin ist nicht ein Kommentar, sondern
`EXECUTOR_MODULES[ExecutorId.ANTHROPIC_BROKER] == ("solvio.provider_broker.anthropic",)`.
Der Tresor nimmt den Modulnamen per `sys._getframe(1)` — deshalb steht
`broker.use(...)` **woertlich in dieser Datei** und in keinem Helfer.

Wer hier NICHT hereinkommt: der schreibende Claude-Builder, Codex, der
Technical Lead, die Schale, der Context Compiler, der Handoff, der
Autopilot-Treiber. Sie sehen ausschliesslich ein leasegebundenes Broker-Token,
das ausserhalb der Rueckschleife nichts oeffnet.

**Was diese Datei anders macht als die OpenAI-Flaeche daneben:**

* **Zwei Anmeldeformen, ausdruecklich unterschieden.** Ein Abo-OAuth-Token
  reist als `Authorization: Bearer`, ein API-Schluessel als `x-api-key`. Welche
  Form gilt, sagt das Feld `kind` im Tresorwert — **nicht** eine Vermutung aus
  dem Praefix des Wertes. Ein Rateschritt an dieser Stelle waere ein stiller
  Fehlschlag mit einem 401, den niemand erklaeren kann.
* **Die Abfragezeichenkette wird verworfen, nicht weitergereicht.** Die
  gemessene CLI ruft `/v1/messages?beta=true`; die Regel des Hauses ist, dass
  kein anfragegesteuerter Inhalt den Anbieter erreicht. Also faellt sie weg.
  Sollte die Live-Messung zeigen, dass der Anbieter sie braucht, wird sie hier
  eine **gepinnte Konstante** — nie ein durchgereichter Wert.
* **Nur `anthropic-version` reist mit**, und nur, wenn sie wie ein Datum
  aussieht. Ein `anthropic-beta` aus dem Kaefig koennte eine Funktion
  einschalten, die niemand bestellt hat; die Betas, die SOLVIO braucht, stehen
  hier gepinnt.
"""
from __future__ import annotations

import json
import re
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("broker")

#: Im Code gepinnt. Kein Wirt, Port, Schema oder Pfadpraefix stammt je aus
#: einer Anfrage des Kaefigs — SSRF ist hier nicht gemildert, sondern
#: strukturell abwesend.
UPSTREAM_ORIGIN = "https://api.anthropic.com"
UPSTREAM_HOST = "api.anthropic.com"

#: Der Tresorplatz. Ein Wert, ein Ref.
SECRET_REF = "secret://anthropic/subscription-token"

#: Die Faehigkeit und die Automatisierung, unter denen geliehen wird.
CAPABILITY = "autopilot.claude_writer"
AUTOMATION_ID = "development-autopilot-v0-6"

#: N8/C4: die Faehigkeit wird je AUFTRAGGEBER gewaehlt. Der Nexus-Arbeiter
#: leiht unter seiner eigenen Faehigkeit — nie unter der des Autopilot-
#: Schreibers. Fehlt sie im Tresor-Grant, faellt der Vorgang mit
#: `credential_denied` (fail-closed), es wird nicht unter fremder Faehigkeit
#: geliehen. Der Auftraggeber kommt aus dem Registratur-Zustand des Brokers,
#: nie aus einem Kopfsatz des Kaefigs.
WORKER_CAPABILITY = "nexus.claude_worker"
WORKER_AUTOMATION_ID = "nexus-c4-claude-worker"
CAPABILITIES = {
    "autopilot-writer-claude": (CAPABILITY, AUTOMATION_ID),
    "autopilot-writer-claude-escalation": (CAPABILITY, AUTOMATION_ID),
    "nexus-worker-claude": (WORKER_CAPABILITY, WORKER_AUTOMATION_ID),
}


def capability_for(principal: str) -> tuple[str, str]:
    """(Faehigkeit, Automatisierung) eines Auftraggebers; leer heisst Schreiber.

    Ein unbekannter Auftraggeber bekommt KEINE Vorgabe: er faellt am Tresor,
    nicht in die Faehigkeit eines anderen.
    """
    if not principal:
        return CAPABILITY, AUTOMATION_ID
    found = CAPABILITIES.get(principal)
    if found is None:
        raise AnthropicAuthError("credential_denied", "principal_not_mapped")
    return found

#: Der EINE Pfad, den Claude Code real braucht (gemessen 2026-09-02:
#: `POST /v1/messages?beta=true`). Keine generische Weiterleitungsflaeche.
FORWARDED_PATHS = ("/v1/messages",)

#: Die Modelle, die diese Flaeche ueberhaupt kennt. Wer welches nennen darf,
#: entscheidet die engere Liste des Auftraggebers (`Caps.allowed_models`).
WRITER_MODEL = "claude-sonnet-5"
WRITER_LARGE_MODEL = "claude-opus-5"
MODEL_ALLOWLIST = frozenset({WRITER_MODEL, WRITER_LARGE_MODEL})

#: Fensterlaengen, die diese Flaeche meldet. Bewusst konservativ und an einer
#: Stelle buchstabiert.
MODEL_CONTEXT_LENGTHS: dict[str, int] = {
    WRITER_MODEL: 200_000,
    WRITER_LARGE_MODEL: 200_000,
}

#: Was aus der eingehenden Anfrage ueberhaupt weiterreisen darf.
FORWARDABLE_REQUEST_HEADERS = ("content-type", "accept")

#: Eine Fassung ist ein Datum, nichts sonst.
_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_VERSION = "2023-06-01"

#: Die Betas, die SOLVIO selbst setzt — je nach Anmeldeform. Gepinnt, nie aus
#: der Anfrage. Die OAuth-Zeile ist die, die ein Abo-Token ueberhaupt gueltig
#: macht; ob der Anbieter sie in dieser Form verlangt, ist die eine offene
#: Live-Messung (Vertrag §9).
OAUTH_BETA = "oauth-2025-04-20"

#: Die Beta-Marken, die Claude Code selbst mitschickt — **gemessen, nicht
#: durchgereicht**.
#:
#: Der erste Live-Beweis am 2026-09-02 endete an
#: `400 context_management: Extra inputs are not permitted`: die CLI schickt
#: ein Feld `context_management` im Rumpf, und der Anbieter nimmt es nur an,
#: wenn die passende Beta-Marke im Kopf steht. Der Entwurf hatte
#: `anthropic-beta` ganz verworfen — eine Marke aus dem Kaefig koennte eine
#: Funktion einschalten, die niemand bestellt hat.
#:
#: Beides bleibt wahr, und die Loesung ist eine **geschlossene Liste**: was
#: hier steht, darf mitreisen; alles andere faellt weg. Gemessen am
#: 2026-09-02 an CLI 2.1.258 gegen einen lokalen Lauscher, ohne Anmeldung.
#: Eine neue Marke einer kuenftigen CLI reist NICHT mit — sie faellt auf, und
#: dann wird sie hier eingetragen oder eben nicht.
CLIENT_BETAS = frozenset({
    "claude-code-20250219",
    "context-management-2025-06-27",
    "effort-2025-11-24",
    "interleaved-thinking-2025-05-14",
    "mid-conversation-system-2026-04-07",
    "prompt-caching-scope-2026-01-05",
    "thinking-token-count-2026-05-13",
    "advisor-tool-2026-03-01",
})

KIND_OAUTH = "subscription_oauth"
KIND_API_KEY = "api_key"
KNOWN_KINDS = (KIND_OAUTH, KIND_API_KEY)


class AnthropicAuthError(RuntimeError):
    """Die Anmeldung fehlt, ist gesperrt oder unlesbar. Genau ein Grund."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def target_url(canonical: str) -> str:
    """`UPSTREAM_ORIGIN` plus der EINE Pfad aus dem Pfadtor."""
    if canonical not in FORWARDED_PATHS:
        raise ValueError("path not in the anthropic allowlist")
    return UPSTREAM_ORIGIN + canonical


def context_length(model: str) -> int:
    return MODEL_CONTEXT_LENGTHS.get(str(model or ""), min(
        MODEL_CONTEXT_LENGTHS.values()))


class AnthropicUpstream:
    """Leiht die Anmeldung je Anfrage und baut den ausgehenden Kopfsatz.

    Der Wert lebt im Kontextmanager des Tresors, wandert in genau einen
    Kopfzeilenwert und ist danach fort. Er steht in keinem Log, keiner
    Ausnahme, keinem Buch und keinem `argv`.
    """

    def __init__(self, broker: Any = None) -> None:
        #: Der Tresor-Broker. `None` heisst: keine Anmeldung — und das ist
        #: eine ehrliche Lage (`no_credential`), kein Absturz.
        self._vault = broker

    # -- Zustand, ohne den Wert zu beruehren --------------------------------

    def configured(self) -> bool:
        """Ob ueberhaupt eine Anmeldung hinterlegt ist. Ohne sie zu oeffnen."""
        if self._vault is None:
            return False
        try:
            return bool(self._vault.exists(SECRET_REF))
        except Exception:  # noqa: BLE001 - ein kaputter Tresor ist kein Absturz
            return False

    # -- Der eine Leihvorgang ------------------------------------------------

    def outbound_headers(self, inbound: Any, *, principal: str = "") -> dict[str, str]:
        """Ein **frischer** Kopfsatz mit der echten Anmeldung.

        Nichts wird durchgereicht, was nicht ausdruecklich erlaubt ist.
        Insbesondere faellt der eingehende `Authorization` UND `x-api-key` weg
        — dort traegt der Kaefig sein Broker-Token, und das hat draussen
        nichts zu suchen.

        `principal` ist der Auftraggeber des Leases (Registratur-Zustand des
        Brokers); er waehlt die Faehigkeit, unter der der Tresor geoeffnet
        wird. Ohne Angabe gilt der Autopilot-Schreiber (bestehende Aufrufer).

        `broker.use(...)` steht hier woertlich: der Tresor nimmt den
        Modulnamen des Aufrufers per Stack-Inspektion, und in einem Helfer
        stuende dort der Helfer.
        """
        if self._vault is None:
            raise AnthropicAuthError("no_credential", "kein Tresor gebunden")

        from solvio.capabilities import policy as AP
        from solvio.secret_vault import broker as B
        from solvio.secret_vault import context as SC
        from solvio.secret_vault import policy as VP

        capability, automation_id = capability_for(principal)
        try:
            with SC.bound(SC.UseContext(
                    origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                    capability=capability, automation_id=automation_id)):
                # Das Ziel ist die HERKUNFT, nicht der blosse Wirt: der
                # Tresor liest `api.anthropic.com` nicht als Ziel, weil ein
                # Hostname nicht sagt, ob verschluesselt. Gemessen, nicht
                # angenommen — `admin.add` wies genau das ab.
                with self._vault.use(SECRET_REF,
                                     executor=VP.ExecutorId.ANTHROPIC_BROKER,
                                     target=UPSTREAM_ORIGIN) as material:
                    payload = _payload(material.plaintext())
                    return _headers(payload, inbound)
        except AnthropicAuthError:
            raise
        except B.SecretDenied as exc:
            raise AnthropicAuthError("credential_denied",
                                     exc.reason.value) from None
        except B.SecretUnavailable:
            raise AnthropicAuthError("vault_unavailable") from None

    def session(self) -> Any:
        """Ein Klient, der weder der Umgebung noch einem Umzug folgt."""
        import aiohttp

        from solvio.provider_broker import upstream as up

        timeout = aiohttp.ClientTimeout(total=up.TOTAL_TIMEOUT,
                                        sock_connect=up.CONNECT_TIMEOUT)
        return aiohttp.ClientSession(timeout=timeout, trust_env=False)


# -- Innen ------------------------------------------------------------------

def _payload(raw: str) -> dict[str, str]:
    """Der Tresorwert als geprueftes Wortbuch — oder eine benannte Absage."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise AnthropicAuthError("credential_malformed", "kein JSON") from None
    if not isinstance(data, dict):
        raise AnthropicAuthError("credential_malformed", "kein Objekt")
    kind = str(data.get("kind") or "")
    token = str(data.get("token") or "")
    if kind not in KNOWN_KINDS:
        # Nicht raten. Eine unbekannte Form ist eine Absage, kein Versuch.
        raise AnthropicAuthError("credential_malformed", f"kind={kind[:24]!r}")
    if not token:
        raise AnthropicAuthError("credential_malformed", "leerer Wert")
    return {"kind": kind, "token": token}


def _headers(payload: dict[str, str], inbound: Any) -> dict[str, str]:
    """Der ausgehende Kopfsatz. Der einzige Ort, an dem der Wert steht."""
    headers = {"Host": UPSTREAM_HOST,
               "anthropic-version": _version(inbound)}
    betas = _betas(inbound)
    if payload["kind"] == KIND_OAUTH:
        headers["Authorization"] = f"Bearer {payload['token']}"
        betas.insert(0, OAUTH_BETA)
    else:
        headers["x-api-key"] = payload["token"]
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    for name in FORWARDABLE_REQUEST_HEADERS:
        value = _header(inbound, name)
        if value:
            headers[name.title()] = value
    return headers


def _betas(inbound: Any) -> list[str]:
    """Die Beta-Marken der Anfrage, gefiltert gegen die geschlossene Liste.

    Reihenfolge und Doppelungen des Kaefigs zaehlen nicht: gebaut wird eine
    neue, sortierte Liste aus dem, was erlaubt ist. Was nicht in
    `CLIENT_BETAS` steht, faellt weg — leise, aber nicht heimlich: die Liste
    ist der Ort, an dem eine neue Marke besprochen wird.
    """
    roh = _header(inbound, "anthropic-beta")
    if not roh:
        return []
    gefunden = {t.strip() for t in roh.split(",") if t.strip()}
    return sorted(gefunden & CLIENT_BETAS)


def _version(inbound: Any) -> str:
    value = _header(inbound, "anthropic-version")
    return value if _VERSION.match(value) else DEFAULT_VERSION


def _header(inbound: Any, name: str) -> str:
    try:
        return str(inbound.headers.get(name, "") or "").strip()
    except AttributeError:
        return ""
