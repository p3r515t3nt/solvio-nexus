"""Die Tore und die Weiterleitung — alles, was der Kaefig ueberhaupt erreicht.

Sieben Tore, alle fail-closed, in dieser Reihenfolge:

1. `Authorization: Bearer` gegen die Registratur (`hmac.compare_digest`)
   → Auftraggeber und Generation. Sonst `401`.
2. Ein offenes, nicht abgelaufenes Lease dieses Auftraggebers. Sonst `403`.
3. Kappen gegen die **bereits gebuchte** Summe. Sonst `429`.
4. Pfad in der Freigabeliste — **ein** kanonischer Wert, der zugleich geprueft
   und weitergeleitet wird. Sonst `404`.
5. Modelltor auf dem vollstaendig gepufferten Rumpf. Sonst `403`.
5b. Anbieterseitige Werkzeuge (`tools[].type != "function"`) gegen
   `Caps.allowed_provider_tools` DIESES Auftraggebers — Voreinstellung die
   LEERE Menge. Sonst `403` mit `provider_tool_not_allowed`.
6. Tokenkappe gegen die Schaetzung; reisst sie, `429` **ohne** Anruf nach
   draussen. Sonst wird die Schaetzung jetzt gebucht.
7. Weiterleiten mit **frischem** Kopfsatz.

`GET /v1/models` durchlaeuft **nur** Tor 1 und Tor 3 und wird dann lokal
beantwortet. Wer die Nummernfolge woertlich implementiert, liefert nie eine
Modellliste: die zweielementige Pfad-Freigabeliste kennt `/v1/models` nicht, und
ein `GET` hat keinen Rumpf fuer das Modelltor.

**Warum die Antwort lokal ist und nicht abgelehnt wird.** Der angeheftete Hermes
haelt eine Rueckschleifen-Basis fuer einen **lokalen** Anbieter
(`model_metadata.py:3004`) und loest die Fensterlaenge deshalb ueber
`/v1/models` auf statt aus seinem Katalog. Ein `401` oder `403` bricht dabei nur
die eine Sonde ab und nicht die Kaskade — abgelehnt wird also nichts gewonnen.
Beantwortet der Broker die Frage dagegen selbst und **mit** Fensterlaenge, endet
die Kaskade sofort, und keine Sonde geht nach draussen.

**Die Abdrucksonden muessen `404` bekommen — das ist tragend, nicht kosmetisch.**
`detect_local_server_type` (`:985`, Wasserfall `:1040-1080`) fragt der Reihe nach
`/api/v1/models`, `/api/tags`, `/v1/props`, `/props` und `/version`. Wer auf
`/api/v1/models` mit `200` antwortet, wird als **LM Studio** eingestuft und
danach mit einer voellig anderen Rumpfform gelesen (`payload["models"][]` mit
`loaded_instances[].config.context_length`, `:1315-1323`) — die
`context_length` der OpenAI-Huelle waere dann unsichtbar. Ein `owned_by`
von `llamacpp` loest ebenso einen Sonderweg aus (`:1396-1414`); deshalb traegt
die Antwort `solvio-provider-broker`.

**Der Rumpf wird vollstaendig gepuffert, und das ist eine Kostenstelle.** Ein
sauberes Modelltor kann nicht anders: eine inkrementelle Suche nach `model`
waere durch doppelte JSON-Schluessel, einen Koeder in einer Zeichenkette oder ein
`model` am Ende zu schlagen — und damit kein Tor. Die **Antwort** stroemt
weiterhin unveraendert.

**Die Antwort wird byte-treu durchgereicht.** Der Verbraucher im Kaefig baut
seine Ausgabe aus den einzelnen `response.output_item.done`-Ereignissen und
liest `response.output` aus dem Abschlussereignis **nie**. Ein Proxy, der den
Strom uebersetzt oder neu rahmt, verliert Werkzeugaufrufe **still** — es gibt
keine Ausnahme, weil ein Abschlussereignis ja ankam. Beobachtet wird nur, was
fuer Verbrauch und Status noetig ist; veraendert wird nichts.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio.provider_broker import upstream as up
from solvio.provider_broker.ledger import Entry

log = get_logger("broker")

#: Die einzigen zwei Pfade, die ueberhaupt nach draussen gehen.
#:
#: `/v1/chat/completions` steht hier nicht aus Bequemlichkeit: der Codex-Umweg
#: des angehefteten Hermes ist BEDINGT. Er verlangt, dass der Aufrufer
#: `api_mode="codex_responses"` mitgibt, und dieser Wert reist nur in der
#: Turn-Bindung. Ein Hilfsaufrufer ohne Turn-Bindung erzeugt eine **echte**
#: ausgehende `POST /v1/chat/completions` — auf dem Draht gemessen. Ein Broker,
#: der nur `/v1/responses` kennt, beantwortete echten Verkehr mit `404`.
FORWARDED_PATHS = ("/v1/responses", "/v1/chat/completions")

#: Lokal beantwortet, nie weitergeleitet.
MODELS_PATH = "/v1/models"

#: Die zwei Modellnamen, die der Broker ueberhaupt kennt. Ein Name steht hier,
#: damit er buchstabiert an genau einer Stelle steht — nicht, damit ihn jeder
#: Auftraggeber bekommt. **Wer welches Modell anfordern darf, entscheidet die
#: Allowlist des Auftraggebers** (`Caps.allowed_models`, `session.py`), und die
#: ist enger als diese Menge: `gpt-5.4` erreicht nur die zwei Core-gehaltenen
#: Eskalations-Auftraggeber, nie den Hermes-Kaefig.
MINI_MODEL = "gpt-5.4-mini"
LARGE_MODEL = "gpt-5.4"

#: Was der Broker ueberhaupt anfordern kann. Kein `*`, keine Praefixsuche.
MODEL_ALLOWLIST = frozenset({MINI_MODEL, LARGE_MODEL})

#: Die Fensterlaengen, die der Broker je Modell meldet.
#:
#: **Diese Werte sind nachgeschlagen, nicht uebernommen** — die Uebergabe nannte
#: fuer Mini 272 000, und das ist fuer SOLVIOs Weg die falsche Tabelle. Im
#: angehefteten Hermes stehen fuer jeden Namen mehrere Zahlen, und sie gehoeren
#: zu verschiedenen ZUGAENGEN:
#:
#: * `DEFAULT_CONTEXT_LENGTHS["gpt-5.4-mini"] = 400000`
#:   (`agent/model_metadata.py:453`) — der **direkte OpenAI-Zugang**.
#: * `DEFAULT_CONTEXT_LENGTHS["gpt-5.4"] = 1050000` (`:454`, Kommentar dort:
#:   „GPT-5.4, GPT-5.4 Pro (1.05M context)") — derselbe direkte Zugang.
#:   **Beim Bau von Cognitive Router V1 an derselben angehefteten Quelle
#:   gemessen**, bevor `gpt-5.4` ueberhaupt anforderbar wurde.
#: * `_CODEX_OAUTH_CONTEXT_FALLBACK["gpt-5.4-mini"] = 272_000` (`:2394`) und
#:   `["gpt-5.4"] = 272_000` (`:2399`) — die **ChatGPT-Codex-OAuth**-Kappe. Der
#:   Kommentar ueber der Tabelle (`:2376-2380`) sagt es selbst: „der direkte
#:   OpenAI-Zugang hat groessere Grenzen fuer dieselben Namen, Codex OAuth
#:   kappt tiefer".
#: * `_CODEX_OAUTH_VERIFIED_ABOVE_ADVERTISED_EXACT["gpt-5.4"] = 900_000`
#:   (`:2430`) — eine dritte Zahl, und auch sie gilt nur dem OAuth-Weg: sie
#:   greift laut Kommentar NUR, wenn der aufgeloeste Wert genau die veraltete
#:   272-000-Anzeige ist.
#:
#: SOLVIO spricht `provider=openai-api` mit einem gewoehnlichen Schluessel gegen
#: `api.openai.com` — **nicht** ueber Codex OAuth. Also gilt die erste Tabelle,
#: fuer beide Namen. Drei Zahlen fuer drei Zugaenge; die richtige ist die des
#: Zugangs, den man benutzt. 272 000 zu melden waere eine stille Verkleinerung
#: des Fensters, die niemand bestellt hat und die als schlechtere Arbeit
#: sichtbar wuerde.
MODEL_CONTEXT_LENGTHS: dict[str, int] = {
    MINI_MODEL: 400_000,
    LARGE_MODEL: 1_050_000,
}

#: Die groesste bekannte Fensterlaenge. Sie ist der Rand, gegen den ein
#: `max_output_tokens` geprueft wird, wenn kein Modell dazu bekannt ist — nie
#: `0` und nie `None`: eine Schranke, die zu `0` ausrechnet, laesst jede Angabe
#: durch, und das ist genau die Klasse Fehler, gegen die der `bool`-Riegel in
#: `estimate_tokens` schon einmal geschrieben wurde.
MAX_MODEL_CONTEXT_LENGTH = max(MODEL_CONTEXT_LENGTHS.values())


def context_length(model: str) -> int:
    """Die Fensterlaenge eines Modells — oder die groesste bekannte."""
    return MODEL_CONTEXT_LENGTHS.get(str(model or ""), MAX_MODEL_CONTEXT_LENGTH)


#: Obergrenze eines Rumpfes — **gemessen, nicht geraten**.
#:
#: Der Startwert des Entwurfs war 64 MiB und ausdruecklich eine Kostenstelle auf
#: Abruf. Die Live-Abnahme hat den groessten tatsaechlichen `request_bytes`
#: abgelesen: **116 073 Bytes** (≈113 KiB, ein Projektkenner-Lauf mit voller
#: Mappe). Ein fensternahes Gespraech am 400k-Modell liegt bei grob 1,6 MB.
#: 8 MiB traegt also das Fuenffache eines vollen Fensters und immer noch das
#: Siebzigfache des gemessenen Groesstwerts — und ist drei Groessenordnungen
#: naeher an der Wirklichkeit als 64 MiB.
MAX_BODY_BYTES = 8 * 1024 * 1024
# 20 MB Dokumentbytes wachsen als base64 auf knapp 26,7 MB. Diese groessere
# Transportkappe wird im Service ausschliesslich fuer `document-ask` gewaehlt.
MAX_DOCUMENT_BODY_BYTES = 27 * 1024 * 1024

#: Und die Summe ueber ALLE Auftraggeber. Ohne sie duerften sechs gleichzeitige
#: Anfragen sechsmal die Einzelgrenze belegen. Genau sechs ist die Zahl
#: gleichzeitiger Anfragen, also ist das die passende Summe.
MAX_TOTAL_BUFFERED_BYTES = 6 * MAX_BODY_BYTES

#: Wie viel Ausgabe veranschlagt wird, wenn der Rumpf keine Obergrenze nennt.
#: Die Live-Abnahme vergleicht `estimated` gegen `reported` und korrigiert.
DEFAULT_OUTPUT_ESTIMATE = 4_096

#: Grob, aber in die richtige Richtung: vier Bytes je Token. Die Schaetzung darf
#: danebenliegen — sie wird durch das gemeldete `usage` ERSETZT, sobald es
#: eintrifft. Was sie nicht darf, ist null sein.
BYTES_PER_TOKEN = 4


class BodyRejected(RuntimeError):
    """Der Rumpf kommt nicht durch das Modelltor."""

    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def models_payload(*, now: float,
                   allowed: frozenset[str] | None = None) -> dict[str, Any]:
    """Die lokale Modellliste — OpenAI-Huelle plus Fensterlaenge.

    **Je Auftraggeber.** Ohne diesen Filter waere die eine authentifizierte,
    lease-freie Route die Stelle, an der ein Kaefig das Fenster eines
    Modells erfaehrt, das er nie anfordern darf — und die Zusage
    „geschlossene Menge JE AUFTRAGGEBER" waere ausgerechnet dort unwahr.

    Mehrere Schluessel tragen denselben Wert, und das ist kein Rauschen:
    `_CONTEXT_LENGTH_KEYS` (`agent/model_metadata.py:652-665`) ist eine
    **Menge, keine Rangfolge**. `_extract_first_int` (`:1116-1125`) laeuft die
    Schluessel der Antwort in ihrer eigenen Einfuegereihenfolge ab und nimmt den
    ersten, der passt — welcher Alias greift, entscheidet also unser Rumpf und
    nicht seine Fassung. Weil alle denselben Wert tragen, kann das Ergebnis
    nicht davon abhaengen.

    Der Wert muss ausserdem in `[1024, 10_000_000]` liegen (`:1102-1114`), sonst
    wird der Schluessel uebersprungen. Und eine Liste mit **einem** Eintrag
    bindet auch ohne genauen Namenstreffer (`:1445-1450`).
    """
    return {
        "object": "list",
        "data": [{
            "id": name,
            "object": "model",
            "created": int(now),
            "owned_by": "solvio-provider-broker",
            "context_length": context_length(name),
            "max_context_length": context_length(name),
            "context_window": context_length(name),
            "max_context_window_tokens": context_length(name),
        } for name in sorted(allowed if allowed is not None else MODEL_ALLOWLIST)],
    }


def parse_model(raw: bytes,
                *, allowed: frozenset[str] | None = None,
                ) -> tuple[str, dict[str, Any]]:
    """Liest **genau ein** `model` von der obersten Ebene — oder weist zurueck.

    Kein Regex, keine Teilzeichenkette, kein Erstfund. Der Rumpf wird mit einem
    echten JSON-Dekoder gelesen, und die Paare der obersten Ebene werden
    gezaehlt, BEVOR das Woerterbuch daraus gebaut wird. Das ist der Unterschied,
    auf den es ankommt: `json.loads` nimmt bei einem doppelten Schluessel
    klaglos den letzten, ein Erstfundscanner den ersten — wer den Rumpf selbst
    baut, spielte die beiden gegeneinander aus.

    Der aeusserste Aufruf des `object_pairs_hook` ist der letzte: ein JSON-Baum
    wird von innen nach aussen fertig. Deshalb ist `seen[-1]` die oberste Ebene
    und nicht irgendein verschachtelter Koeder.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BodyRejected("model_not_allowed") from None

    seen: list[list[tuple[str, Any]]] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen.append(list(pairs))
        return dict(pairs)

    try:
        value = json.loads(text, object_pairs_hook=hook)
    except (ValueError, RecursionError):
        raise BodyRejected("model_not_allowed") from None

    if not isinstance(value, dict) or not seen:
        raise BodyRejected("model_not_allowed")

    top = seen[-1]
    models = [item for key, item in top if key == "model"]
    if len(models) != 1:
        # Kein `model`, oder mehr als eines. Beide sind eine Ablehnung; bei
        # zweien wird ausdruecklich NICHT das freigegebene ausgewaehlt.
        raise BodyRejected("model_not_allowed")
    chosen = models[0]
    # Die Menge des Auftraggebers, nicht die des Hauses. Ohne `allowed` gilt
    # weiter, was der Broker ueberhaupt kennt — das ist die Lesart der
    # Aufrufer, die kein Auftraggeberobjekt haben (Tests, Werkzeuge).
    permitted = allowed if allowed is not None else MODEL_ALLOWLIST
    if not isinstance(chosen, str) or chosen not in permitted:
        raise BodyRejected("model_not_allowed")
    return chosen, value


def check_provider_tools(body: dict[str, Any], *,
                         allowed: frozenset[str]) -> None:
    """Das Broker-Tor fuer anbieterseitige Werkzeuge — deterministisch, auf
    dem bereits geparsten Rumpf.

    Jeder Eintrag in `tools`, dessen `type` NICHT `function` ist, ist
    anbieterseitig (der Anbieter fuehrt ihn selbst aus — `web_search`,
    `code_interpreter`, `file_search`, `computer_use`, `image_generation`,
    `mcp`, ...) und muss in `allowed` stehen. `function` bleibt immer erlaubt:
    das ist klientenseitig, der Kaefig wertet das Ergebnis selbst aus, und
    kein Auftraggeber bekommt dadurch Anbieterzugriff.

    Kein Eintrag wird einzeln genannt — die Menge wird EINGESCHLOSSEN, nicht
    eine verbotene Liste AUSGESCHLOSSEN. Damit ist jedes kuenftige
    anbieterseitige Werkzeug automatisch gesperrt, bis ein Auftraggeber es
    ausdruecklich bekommt.
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "function":
            continue
        if not isinstance(kind, str) or kind not in allowed:
            raise BodyRejected("provider_tool_not_allowed")


def check_anthropic_provider_tools(body: dict[str, Any], *,
                                   allowed: frozenset[str]) -> None:
    """Dasselbe Tor fuer die Anthropic-Messages-Flaeche (Review Runde 16, B16-1 —
    die Flaeche hatte keins; der Claude-Auftragsarbeiter haette aus dem
    „netzlosen" Kaefig ein Anbieter-`web_search` mitschicken koennen).

    Ein Werkzeug OHNE `type` oder mit `type: custom` ist klientenseitig
    (`input_schema`; gemessen 20.09.2026: die CLI meldet Bash/Edit/Read so).
    Jeder andere `type` ist anbieterdefiniert — `web_search_20250305`,
    `web_fetch_…`, `code_execution_…`, auch `computer_…`/`text_editor_…`/
    `bash_…` — und muss als voller Typ ODER als Familie ohne Datumssuffix in
    `allowed` stehen. `mcp_servers` (der MCP-Connector des Anbieters, Netz auf
    seiner Seite) braucht `mcp`. Eingeschlossen, nie ausgeschlossen.
    """
    tools = body.get("tools")
    if isinstance(tools, list):
        for entry in tools:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind is None or kind == "custom":
                continue
            if not isinstance(kind, str):
                raise BodyRejected("provider_tool_not_allowed")
            family = re.sub(r"_[0-9]{8}$", "", kind)
            if kind not in allowed and family not in allowed:
                raise BodyRejected("provider_tool_not_allowed")
    if body.get("mcp_servers") and "mcp" not in allowed:
        raise BodyRejected("provider_tool_not_allowed")


def check_input_shape(body: dict[str, Any], *, allowed_mime: frozenset[str]) -> None:
    """Erlaubt im Dokumentpfad nur Text und eingebettete, erlaubte Dateien."""
    inputs = body.get("input")
    if not isinstance(inputs, list) or not inputs:
        raise BodyRejected("input_shape_not_allowed")
    for item in inputs:
        if not isinstance(item, dict):
            raise BodyRejected("input_shape_not_allowed")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise BodyRejected("input_shape_not_allowed")
        for part in content:
            if not isinstance(part, dict):
                raise BodyRejected("input_shape_not_allowed")
            kind = part.get("type")
            if kind == "input_text":
                if not isinstance(part.get("text"), str):
                    raise BodyRejected("input_shape_not_allowed")
                continue
            if kind != "input_file":
                raise BodyRejected("input_shape_not_allowed")
            file_data = part.get("file_data")
            if not isinstance(file_data, str):
                raise BodyRejected("input_shape_not_allowed")
            prefix, separator, encoded = file_data.partition(";base64,")
            if separator != ";base64," or not prefix.startswith("data:") or not encoded:
                raise BodyRejected("input_shape_not_allowed")
            mime = prefix[5:]
            if mime not in allowed_mime:
                raise BodyRejected("input_shape_not_allowed")
            try:
                base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError, TypeError):
                raise BodyRejected("input_shape_not_allowed") from None


def estimate_tokens(raw: bytes, body: dict[str, Any], *, model: str = "") -> int:
    """Die Vorbelastung aus dem ohnehin gepufferten Rumpf.

    Sie muss nicht genau sein — sie wird durch das gemeldete `usage` ersetzt,
    sobald es eintrifft. Sie muss nur da sein: `usage` kommt erst im letzten
    Stromereignis und bei abgeklemmter Verbindung nie, und eine Kappe, die auf
    Beobachtetes wartet, waere mit einem abgebrochenen Strom auf null zu setzen.
    """
    approx_input = max(1, (len(raw) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN)
    requested = 0
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = body.get(key)
        # `isinstance(True, int)` ist in Python wahr — ohne die
        # `bool`-Ausnahme veranschlagte `max_output_tokens: true` genau EINEN
        # Token. Eine Kappe, die sich mit einem `true` auf null setzen laesst,
        # ist keine.
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 < value <= context_length(model):
            requested = value
            break
    return approx_input + (requested or DEFAULT_OUTPUT_ESTIMATE)


#: Wie eine Buchung zustande kam. Geschlossen — eine Quelle ohne Namen waere
#: eine Zahl, deren Herkunft niemand mehr feststellt.
#:
#: * `reported`       — der Anbieter hat `usage` gemeldet. Die Wahrheit.
#: * `measured_bytes` — er hat es NICHT gemeldet, weil der Klient den Strom
#:                      abbrach; gebucht wird, was nachweislich floss: der
#:                      ganze Anfragerumpf (den der Anbieter gelesen hat) und
#:                      die Bytes, die bis zum Abbruch zurueckkamen.
#: * `estimated`      — weder das eine noch das andere ist bekannt. Die
#:                      Vorbelastung bleibt stehen; eine Kappe, die auf
#:                      Beobachtetes wartet, waere mit einem Abbruch auf null
#:                      zu setzen.
TOKEN_SOURCE_REPORTED = "reported"
TOKEN_SOURCE_MEASURED = "measured_bytes"
TOKEN_SOURCE_ESTIMATED = "estimated"


def settled_charge(*, seen: bool, input_tokens: int, output_tokens: int,
                   estimate: int, request_bytes: int, sent_bytes: int,
                   aborted: bool, status: int) -> tuple[int, int, str]:
    """Was am Ende gebucht wird — und woher die Zahl kommt.

    Die Entscheidung steht an EINER Stelle, weil sie eine Entscheidung ist:
    drei Faelle, jeder mit einem Namen, keiner mit einem stillen Ausweg.

    **Der mittlere Fall ist der Fund aus der V0.6-Abnahme (DEBT-0185).** Bricht
    der Klient den Strom ab, kommt `usage` nie — und die Vorbelastung blieb
    stehen: 65 024 Token fuer eine Anfrage, die vielleicht 200 verbraucht hat.
    Bei einem Klienten, der Spekulativanfragen abbricht (Claude Code tut das),
    summiert sich das zu einer Tageskappe, die frueher greift als der echte
    Verbrauch. Gemessen: 22 von 160 Anfragen der B5-Abnahme endeten so.

    Was in diesem Fall trotzdem **sicher bekannt** ist: der Anbieter hat den
    ganzen Anfragerumpf gelesen, und die Bytes, die zurueckflossen, sind
    gezaehlt. Beides wird gebucht. Auf null faellt es nie — ein Abbruch ist
    kein Freifahrtschein.
    """
    if seen:
        return int(input_tokens), int(output_tokens), TOKEN_SOURCE_REPORTED
    if aborted and status < 400:
        eingang = max(1, (int(request_bytes) + BYTES_PER_TOKEN - 1)
                      // BYTES_PER_TOKEN)
        ausgang = (int(sent_bytes) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN
        return eingang, max(0, ausgang), TOKEN_SOURCE_MEASURED
    return int(estimate), 0, TOKEN_SOURCE_ESTIMATED


class UsageSniffer:
    """Liest `usage` im **vorbeifliessenden** Strom mit — ohne ihn anzufassen.

    Es wird nichts gepuffert, was der Kaefig nicht ohnehin bekommt, nichts
    umsortiert und nichts umgeschrieben. Beobachtet wird eine einzige Zahl je
    Richtung. Zeilen ueber der Grenze werden verworfen statt zu wachsen — ein
    Beobachter darf nie zur Speicherkostenstelle werden.
    """

    MAX_LINE = 1024 * 1024

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.input_tokens = 0
        self.output_tokens = 0
        self.seen = False

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line = bytes(self._buffer[:index])
            del self._buffer[:index + 1]
            self._consider(line)
        if len(self._buffer) > self.MAX_LINE:
            del self._buffer[:-self.MAX_LINE]

    def finish(self) -> None:
        """Ein Rumpf ohne Zeilenumbruch — der nicht stroemende Fall."""
        if self._buffer:
            self._consider(bytes(self._buffer))
            self._buffer.clear()

    def _consider(self, line: bytes) -> None:
        if b'"usage"' not in line:
            return
        payload = line.strip()
        if payload.startswith(b"data:"):
            payload = payload[5:].strip()
        if not payload.startswith(b"{"):
            return
        try:
            body = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        for candidate in _usage_candidates(body):
            got = _read_usage(candidate)
            if got is not None:
                self.input_tokens, self.output_tokens = got
                self.seen = True


def _usage_candidates(body: Any) -> list[Any]:
    """`usage` liegt je nach Pfad am Ereignis selbst oder unter `response`."""
    out: list[Any] = []
    if isinstance(body, dict):
        if isinstance(body.get("usage"), dict):
            out.append(body["usage"])
        nested = body.get("response")
        if isinstance(nested, dict) and isinstance(nested.get("usage"), dict):
            out.append(nested["usage"])
    return out


def _read_usage(usage: dict[str, Any]) -> tuple[int, int] | None:
    """Beide Drahtformen: Responses zaehlt `input`/`output`, Chat
    `prompt`/`completion`."""
    for first, second in (("input_tokens", "output_tokens"),
                          ("prompt_tokens", "completion_tokens")):
        left, right = usage.get(first), usage.get(second)
        if isinstance(left, int) and isinstance(right, int):
            return max(0, left), max(0, right)
    return None


@dataclass(eq=False)
class Forward:
    """Eine zugelassene Weiterleitung — damit sie bei Lease-Null endet.

    Ein Lease-Schluss beendet eine bereits zugelassene Weiterleitung NICHT von
    selbst; das ist der Teil, den das Bedrohungsmodell offen benennt. Diese
    Handhabe ist der Abbruch, der ihn schliesst.
    """

    principal: str
    aborted: bool = False
    response: Any = None
    _closers: list[Any] = field(default_factory=list)

    def abort(self) -> None:
        self.aborted = True
        for closer in self._closers:
            try:
                closer()
            except Exception:  # noqa: BLE001 - ein Abbruch darf nie werfen
                pass

    def on_abort(self, closer: Any) -> None:
        self._closers.append(closer)


def ledger_entry(*, principal: str, generation: int, method: str, path: str,
                 outcome: str, lease_id: str = "", task_ref: str = "",
                 model: str = "", status_code: int = 0, request_bytes: int = 0,
                 denied_reason: str = "", input_tokens: int = 0,
                 output_tokens: int = 0, tokens_source: str = "",
                 request_sha256: str = "") -> Entry:
    """Eine Buchzeile bauen — aufgezaehlt, damit nichts mitreist."""
    return Entry(
        principal=principal, generation=generation, method=method, path=path,
        outcome=outcome, lease_id=lease_id, task_ref=task_ref, model=model,
        status_code=status_code, request_bytes=request_bytes,
        denied_reason=denied_reason, input_tokens=input_tokens,
        output_tokens=output_tokens, tokens_source=tokens_source,
        request_sha256=request_sha256)


def body_digest(raw: bytes) -> str:
    """Der Fingerabdruck eines Anfragerumpfs. Nur er, nie der Rumpf.

    Warum ueberhaupt: `request_bytes` allein beweist nur GLEICHE LAENGE. Die
    Live-Messung vom 2026-08-29 zeigte Dreiergruppen mit je 87 802 Byte — dass
    sie byte-identisch waren, liess sich damals nur plausibel machen, nicht
    belegen. Ein Hash belegt es, und er traegt zugleich den Duplikat-Schutz.

    Ein Hash ist keine Aufforderung: aus ihm laesst sich kein Text
    zurueckgewinnen, und er faellt damit nicht unter „was nie ins Buch geht".
    """
    return hashlib.sha256(raw or b"").hexdigest()


def canonical_path(request: Any) -> str:
    """Der EINE Wert, der geprueft und weitergeleitet wird.

    Es wird der rohe Pfad genommen und **woertlich** gegen die Freigabeliste
    gehalten — nicht normalisiert, nicht dekodiert, nicht zusammengesetzt. Damit
    ist eine kodierte Traversierung (`/v1/%2e%2e/admin`) kein Sonderfall, den
    jemand bedacht haben muss, sondern schlicht kein Listeneintrag.

    Eine Abfragezeichenkette wird abgelehnt: sie waere anfragegesteuerter
    Inhalt, der den Anbieter erreicht. Der angeheftete Hermes sendet auf diesen
    beiden Pfaden keine.
    """
    if getattr(request.rel_url, "query_string", ""):
        # Anfragegesteuerter Inhalt, der sonst den Anbieter erreichte. Der
        # angeheftete Hermes sendet auf diesen beiden Pfaden keine — also ist
        # eine Absage hier gefahrlos und eine stille Entsorgung waere die
        # schlechtere Wahl: sie machte den Satz „geprueft und weitergeleitet
        # sind derselbe Wert" unwahr.
        return ""
    raw = getattr(request.rel_url, "raw_path", "") or request.path
    if "?" in raw:
        return ""
    return raw


def upstream_url(path: str) -> str:
    return up.target_url(path)


def now() -> float:
    return time.time()
