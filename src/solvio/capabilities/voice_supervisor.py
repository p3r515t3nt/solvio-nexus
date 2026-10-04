"""Der starke Antwortweg fuer das Sprachgespraech -- Chat-Supervisor-Muster.

**Wofuer.** Das Sprachmodell fuehrt das Gespraech: es hoert zu, unterbricht
sauber, hat den Ton. Fuer eine Frage, die wirklich Denkarbeit braucht, gibt es
sie hier ab -- mit dem relevanten Gespraechsverlauf einschliesslich frueherer
Rechercheergebnisse und deren Quellen -- und spricht die Antwort aus.

**Was das NICHT ist.** Keine Routingstufe. Diese Faehigkeit entscheidet nicht,
WOHIN etwas geht; sie formuliert den INHALT. Es gibt keinen Einschaetzer davor
und keinen dahinter.

**Warum eigener Auftraggeber.** Der Auftraggeber IST der Zugang, der Modellname
ist keine Berechtigung -- dieselbe Regel wie im ganzen Haus. `gpt-5.4` war
bisher ausschliesslich ueber drei Eskalations-Auftraggeber erreichbar, deren
Token nur Core-Code praegt. Ein Sprachgespraech braucht einen eigenen, weil es
ein eigenes Verbrauchsmuster hat: selten, aber im Turn, und mit einer harten
Zeitgrenze, die keine der Eskalationsbahnen kennt. Zwei Bahnen mit einer Kappe
waeren keine Kappe.

**Die Grenzen, alle vier ausdruecklich:**

* **Modell** -- `gpt-5.4`, festgelegt, nicht aus dem Aufruf waehlbar.
* **Eingabe** -- Frage und Zusammenhang zusammen hoechstens `MAX_INPUT_CHARS`.
  Laenger wird ABGELEHNT, nicht stillschweigend gekuerzt: eine halbierte Frage
  beantwortet eine andere Frage.
* **Ausgabe** -- ZWEI Schrauben, wie die Anbieterdoku sie vorsieht:
  `reasoning.effort` steuert das Denken, `text.verbosity` die Laenge der
  sichtbaren Antwort. `max_output_tokens` ist keine von beiden, sondern der
  Notausgang darueber — es deckelt Denken UND Text zusammen, und wer das
  verwechselt, bekommt bei erschoepftem Budget einen LEEREN Text mit
  `incomplete_details.reason=max_output_tokens` und zahlt trotzdem.
* **Aufrufzahl und Zeit** -- `MAX_ATTEMPTS = 1` (kein Wiederholungsversuch: ein
  Gespraechsturn hat keine zweite Frist) und `REQUEST_TIMEOUT`. Die Tageszahl
  steht in den Kappen des Auftraggebers, nicht hier.

**Keine anbieterseitigen Werkzeuge.** `allowed_provider_tools` ist LEER. Diese
Faehigkeit sucht nicht, liest keine Dateien, fuehrt keinen Code aus. Braucht
die Antwort frische Fakten, ruft das Gespraechsmodell VORHER `research_quick`
-- und das Ergebnis steht dann samt Quellen im uebergebenen Zusammenhang. So
bleibt die Suchbelegpflicht dort, wo sie gehaertet wurde.

**Und der wunde Punkt des Vorbilds, den SOLVIO nicht uebernimmt.** Im Beispiel
von OpenAI ist die Supervisor-Antwort faktisch Autoritaet: kein Freigabe-Haken,
keine Rundenobergrenze, und die Zusage "woertlich wiedergeben" steht allein im
Prompt. Hier ist sie INFORMATION -- die Faehigkeit ist `READ_ONLY`, sie kann
nichts ausloesen, und ihr Text ist eine Vorlage fuer das Sprachmodell, kein
Befehl.
"""
from __future__ import annotations

import json
import time
from typing import Any

from solvio.capabilities.contract import (CapabilityDeclined, CapabilitySpec,
                                          ExecutionClass, ExecutorUnavailable)
# **Wiederverwendet, nicht nachgebaut.** `_response_state` liest Zustand,
# `incomplete_details.reason` und `usage.output_tokens_details.reasoning_tokens`
# -- auch in der verschachtelten `response`-Form, die der eigene Broker kennt.
# Sie ist am 07.09.2026 gegen sechs Umschlagformen gehaertet worden; eine
# zweite Lesart waere eine zweite Stelle, an der dieselbe Luecke entsteht.
from solvio.capabilities.research_quick import _response_state
from solvio.logging_setup import get_logger
from solvio.provider_broker.service import VOICE_SUPERVISOR_PRINCIPAL
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("voice_supervisor")

#: Das einzige Modell, das dieser Auftraggeber anfordern darf
#: (`VOICE_SUPERVISOR_CAPS.allowed_models`) -- hier referenziert, nicht
#: abgeschrieben, wie ueberall im Haus.
MODEL = "gpt-5.4"

#: Frage und Zusammenhang zusammen. Grosszuegig genug fuer ein langes
#: Gespraech, eng genug, dass eine Tageskappe etwas bedeutet.
MAX_INPUT_CHARS = 8_000
#: Die Frage allein -- eine, die laenger ist, ist keine Frage mehr.
MAX_QUESTION_CHARS = 1_000
MIN_QUESTION_CHARS = 8

# -- Das Token-Budget: zwei Groessen, nicht eine -------------------------------
#
# **Hier stand ein echter Denkfehler.** Die Kappe war 700 mit der Begruendung
# „das ist eine gesprochene Antwort". Das ist falsch. Die Anbieterdoku sagt
# woertlich, dass `max_output_tokens` „an upper bound for the number of tokens
# that can be generated for a response, **including visible output tokens and
# reasoning tokens**" ist. Bei `effort: medium` haette die Denkphase die 700
# allein aufbrauchen koennen — und dann, ebenfalls woertlich: „This might occur
# before any visible output tokens are produced, meaning you could incur costs
# for input and reasoning tokens without receiving a visible response."
# Der Nutzer haette Stille bekommen, bezahlt.
#     https://developers.openai.com/api/docs/guides/reasoning
#
# **Deshalb steuern hier ZWEI Schrauben, wie die Doku sie vorsieht:**
#
#   `reasoning.effort`  — wieviel gedacht wird
#   `text.verbosity`    — wie lang die SICHTBARE Antwort wird
#
# Die Doku fuehrt beide getrennt: verbosity „influences the length of the
# model's final answer, **as opposed to the length of its thinking**".
# `max_output_tokens` ist keine der beiden — es ist der Notausgang darueber.
#
# **Was tatsaechlich gemessen ist** (12 echte Laeufe am 02. und 07.09.2026,
# `gpt-5.4-mini`, `effort: low`, mit Websuche — die einzigen Zahlen, die dieses
# Haus hat):
#
#     sichtbarer Text   100 - 320 Token   (Mittel ~166)
#     Denkarbeit         83 - 1080 Token   (42 % bis 83 % der Gesamtausgabe)
#
# Der sichtbare Teil ist bemerkenswert stabil; die Denkarbeit schwankt um den
# Faktor 13. Genau deshalb sind es zwei Groessen.

#: Wie lang die GESPROCHENE Antwort werden soll. Die laengste gemessene
#: sichtbare Antwort hatte 320 Token; 400 ist grosszuegig und trotzdem
#: sprechbar. Diese Zahl ist ein WUNSCH — durchgesetzt wird sie von
#: `TEXT_VERBOSITY` und der Anweisung, nicht von der Kappe.
ANTWORT_TOKENS = 400

#: Reserve fuer die Denkarbeit. **Ausdruecklich vorlaeufig.**
#:
#: 2600 ist das 2,4-fache der hoechsten gemessenen Denkarbeit (1080) — aber
#: gemessen wurde am KLEINEREN Modell. Fuer `gpt-5.4` bei `low` hat dieses Haus
#: keine Zahl.
#:
#: **Warum nicht einfach sehr gross.** Weil die Kappe hier NICHT kostenlos ist:
#: der eigene Broker bucht `ceil(Rumpfbytes / 4) + max_output_tokens` VORAB
#: gegen die Tageskappe, und auf dieser Strecke sieht er `usage` nie
#: (gemessen: 20 von 20 Zeilen `tokens_source=estimated`, `out_tokens=0`;
#: `530 B / 4 + 2500 = 2633` geht genau auf). Eine grosszuegige Kappe
#: verbraucht also das Tagesbudget, ob sie genutzt wird oder nicht — DEBT-0248.
DENK_RESERVE = 2_600

#: Was an den Anbieter geht. ABGELEITET, nie von Hand gesetzt — sonst laufen
#: die beiden Groessen wieder auseinander und niemand merkt es.
#:
#: Die Rechnung, die zu den Kappen des Auftraggebers passen MUSS:
#:     Rumpf hoechstens ~9200 B -> ~2300 Token vorgebucht
#:     + 3000                    = ~5300 je Aufruf
#:     x 40 Aufrufe              = ~212 000  <  250 000 Token/Tag
#: Damit bindet die AUFRUFZAHL zuerst, nicht die Tokenkappe. Eine Grenze, die
#: frueher greift als die dokumentierte, ist eine Falle.
MAX_OUTPUT_TOKENS = ANTWORT_TOKENS + DENK_RESERVE

#: Der Denkaufwand. **Vorlaeufig**, mit Kalibrierungsauftrag.
#:
#: `low` ist fuer `gpt-5.4` dokumentiert (die Modellseite nennt
#: `none` (Vorgabe), `low`, `medium`, `high`, `xhigh`) — und die Vorgabe waere
#: `none`, also GAR KEIN Denken. Wer hier nichts setzt, bekommt kein
#: Reasoning-Modell. Die Doku empfiehlt fuer latenzempfindliche Faelle
#: ausdruecklich, mit `low` zu beginnen.
#:     https://developers.openai.com/api/docs/models/gpt-5.4
REASONING_EFFORT = "low"

#: Die Laenge der SICHTBAREN Antwort — die zweite, getrennte Schraube.
#: Werte `low`/`medium`/`high`, Vorgabe `medium`. Wer sie nicht setzt, bekommt
#: die ausfuehrliche Voreinstellung, egal wie niedrig der Denkaufwand steht.
#:     https://developers.openai.com/api/reference/resources/responses/methods/create
TEXT_VERBOSITY = "low"

#: Harte Zeitgrenze je Aufruf. Ein Gespraechsturn, der laenger wartet, ist
#: kein Gespraech mehr.
REQUEST_TIMEOUT = 30.0
#: **Genau ein Versuch.** Ein Turn hat keine zweite Frist -- und ein
#: Wiederholungsversuch, der die Frist sprengt, ist eine Frist, die der Turn
#: nicht hat. (`research_quick` darf zweimal, weil dort ein technischer
#: Aussetzer bei erzwungener Suche haeufiger ist; hier waere es Rateverhalten.)
MAX_ATTEMPTS = 1
#: Wie lange das Lease offen bleiben darf.
LEASE_SECONDS = 60.0

INSTRUCTION = (
    "Du formulierst die inhaltliche Antwort fuer einen deutschen Sprachassistenten. "
    "Antworte auf Deutsch, in ganzen sprechbaren Saetzen, ohne Aufzaehlungszeichen, "
    "ohne Ueberschriften, ohne Markdown und ohne URLs. Fasse dich kurz: zwei bis "
    "fuenf Saetze, nur laenger wenn die Sache es zwingend verlangt.\n\n"
    "Du bekommst den Gespraechsverlauf. Nutze ihn, um Bezuege aufzuloesen. "
    "Stehen darin Rechercheergebnisse mit Quellen, stuetze dich darauf und nenne "
    "die Quelle beim Namen. Steht dort nichts Belastbares, sage offen, was du "
    "nicht weisst -- erfinde keine Zahl, kein Datum und keine Quelle.\n\n"
    "Du hast keine Werkzeuge und keinen Netzzugang. Du kannst nichts nachsehen, "
    "nichts ausloesen und nichts veranlassen. Wenn die Frage eine frische "
    "Recherche braucht, sage genau das in einem Satz."
)

SPECS: dict[str, CapabilitySpec] = {
    "voice_supervisor_answer": CapabilitySpec(
        name="voice_supervisor_answer", version=1,
        execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "question": {"type": "string"},
            "context": {"type": "string"}}, "required": ["question"]},
        executor="inline", timeout=REQUEST_TIMEOUT + 5.0,
        description="Formuliert die inhaltliche Antwort auf eine schwierige "
                    "Frage mit einem starken Textmodell, aus dem uebergebenen "
                    "Gespraechszusammenhang. Ohne Werkzeuge, ohne Netzzugang."),
}


def _payload(question: str, context: str) -> dict[str, Any]:
    inhalt = question if not context else (
        f"==== Gespraechsverlauf ====\n{context}\n\n"
        f"==== Die Frage ====\n{question}")
    return {
        "model": MODEL,
        "input": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": inhalt},
        ],
        # **Leer, nicht weggelassen.** Ein fehlendes Feld ueberlaesst die
        # Entscheidung dem Anbieter; ein leeres sagt sie. Der Broker weist
        # ohnehin jedes anbieterseitige Werkzeug ab -- diese Zeile ist die
        # zweite Schranke, nicht die erste.
        "tools": [],
        "reasoning": {"effort": REASONING_EFFORT},
        # Die zweite Schraube. Sie steht unter `text`, NICHT unter `reasoning`
        # — zwei getrennte Objekte im Rumpf, genau wie in der Doku.
        "text": {"verbosity": TEXT_VERBOSITY},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


#: Die Zustaende, die eine ABGESCHLOSSENE Antwort bedeuten. Eine POSITIVliste,
#: aus demselben Grund wie bei der Kurzrecherche: eine Negativliste muss jeden
#: Fehlerfall im Voraus kennen, und was sie nicht kennt, faellt automatisch auf
#: die gute Seite.
RESPONSE_COMPLETED = frozenset({"completed"})
#: Welche Inhaltsteile ueberhaupt Antworttext sind.
ANSWER_PARTS = frozenset({"output_text"})


def _antworttext(umschlag: dict[str, Any]) -> str:
    """Nur der Text. Zustand und Verbrauch liest `_response_state`."""
    daten = umschlag.get("data") or umschlag
    if isinstance(daten.get("response"), dict):
        daten = daten["response"]
    teile: list[str] = []
    for eintrag in daten.get("output") or []:
        if not isinstance(eintrag, dict) or eintrag.get("type") != "message":
            continue
        for stueck in eintrag.get("content") or []:
            if isinstance(stueck, dict) and stueck.get("type") in ANSWER_PARTS:
                teile.append(str(stueck.get("text") or ""))
    return "".join(teile).strip()


async def _post_broker(payload: dict, *, token: str, port: int = 0,
                       timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST an den Broker -- woertlich der Weg aus `research_quick._post_broker`."""
    import aiohttp

    from solvio.provider_broker.service import configured_port

    chosen = int(port) or configured_port()
    url = f"http://127.0.0.1:{chosen}/v1/responses"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    limit = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=limit) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status != 200:
                    reason = _denied_reason(body) or f"broker_{response.status}"
                    return {"ok": False, "reason": reason}
                return {"ok": True, "data": json.loads(body or "{}")}
    except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist keine Erlaubnis
        log.warning("voice_supervisor.post_failed", kind=type(exc).__name__)
        return {"ok": False, "reason": "voice_supervisor_transport_failed"}


def _denied_reason(body: str) -> str:
    """Der Grund des Brokers wird GELESEN, nicht geraten."""
    try:
        parsed = json.loads(body or "")
    except ValueError:
        return ""
    fehler = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(fehler, dict) and fehler.get("type") == "solvio_broker":
        return str(fehler.get("code") or "")
    return ""


class VoiceSupervisorCapabilities:
    """Der Handler. Der Broker wird SPAET gelesen, nie beim Anhaengen gemerkt.

    Derselbe Grund wie bei der Kurzrecherche: der Broker haengt am Server, nicht
    am Dispatcher, und entsteht NACH dieser Registrierung. Ein einmal gemerktes
    `None` waere fuer die gesamte Laufzeit `broker_absent`.
    """

    def __init__(self, dispatcher: Any, *, transport: Any = None,
                 port: int = 0) -> None:
        self.dispatcher = dispatcher
        self._transport = transport if transport is not None else _post_broker
        self._port = port

    @property
    def broker(self) -> Any:
        return getattr(self.dispatcher, "provider_broker", None)

    async def answer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments.get("question", "") or "").strip()
        context = str(arguments.get("context", "") or "").strip()

        if len(question) < MIN_QUESTION_CHARS:
            raise CapabilityDeclined("question_too_short",
                                     "Was genau soll ich durchdenken?")
        if len(question) > MAX_QUESTION_CHARS:
            raise CapabilityDeclined("question_too_long",
                                     "Das ist zu lang -- bitte kuerzer fassen.")
        # **Ablehnen, nicht kuerzen.** Ein stillschweigend halbierter
        # Zusammenhang beantwortet eine andere Frage als die gestellte, und
        # niemand sieht es. Lieber ein ehrliches Nein.
        if len(question) + len(context) > MAX_INPUT_CHARS:
            raise CapabilityDeclined(
                "context_too_large",
                "Der Zusammenhang ist zu gross fuer diesen Weg.")

        begonnen = time.monotonic()
        umschlag = await self._einmal(question, context)

        if not umschlag.get("ok"):
            grund = str(umschlag.get("reason") or "voice_supervisor_failed")
            log.info("voice_supervisor.transport_failed", reason=grund,
                     context_chars=len(context),
                     elapsed_ms=int((time.monotonic() - begonnen) * 1000))
            fehler = ExecutorUnavailable(grund)
            fehler.reason = grund
            raise fehler

        text = _antworttext(umschlag)
        zustand = _response_state(umschlag.get("data") or umschlag)
        status = zustand["status"]
        sichtbar = max(0, zustand["out_tokens"] - zustand["reasoning_tokens"])

        # **Auf JEDEM Ausgang, nicht nur beim Erfolg.** Der erste echte Lauf
        # soll `DENK_RESERVE` und `ANTWORT_TOKENS` kalibrieren koennen -- und
        # gerade der Fehlschlag traegt die Zahl, die dafuer zaehlt. Am
        # 07.09.2026 lief eine Kurzrecherche 16,9 s ins Leere, und es gab
        # KEINE Stelle im Haus, die den Verbrauch oder den Abbruchgrund kannte.
        log.info("voice_supervisor.response",
                 status=status,
                 # DAS ist die Zahl, die die Reserve kalibriert.
                 incomplete=zustand["incomplete"],
                 incomplete_reported=zustand["incomplete_reported"],
                 chars=len(text),
                 in_tokens=zustand["in_tokens"],
                 out_tokens=zustand["out_tokens"],
                 reasoning_tokens=zustand["reasoning_tokens"],
                 sichtbare_tokens=sichtbar,
                 budget=MAX_OUTPUT_TOKENS, reserve=DENK_RESERVE,
                 effort=REASONING_EFFORT, verbosity=TEXT_VERBOSITY,
                 context_chars=len(context),
                 elapsed_ms=int((time.monotonic() - begonnen) * 1000))

        # **Zustand UND Text, nicht eines von beiden.** Ein abgebrochener
        # Umschlag mit halbem Satz ist keine Antwort, auch wenn Text drinsteht.
        if status not in RESPONSE_COMPLETED:
            # Der haeufigste erwartete Abbruchgrund bekommt eine eigene,
            # ehrliche Meldung -- und einen eigenen maschinenlesbaren Grund,
            # damit die Kalibrierung ihn im Buch findet.
            if zustand["incomplete"] == "max_output_tokens":
                log.warning("voice_supervisor.budget_exhausted",
                            reasoning_tokens=zustand["reasoning_tokens"],
                            budget=MAX_OUTPUT_TOKENS, reserve=DENK_RESERVE)
                raise CapabilityDeclined(
                    "budget_exhausted",
                    "Ich habe mich daran festgedacht und bin nicht "
                    "fertig geworden.")
            raise CapabilityDeclined(
                "answer_incomplete",
                "Ich habe darueber nachgedacht, aber keine vollstaendige "
                "Antwort zusammenbekommen.")
        if not text:
            raise CapabilityDeclined(
                "answer_empty",
                "Dazu faellt mir gerade nichts Belastbares ein.")

        return {
            "answer": text,
            "model": MODEL,
            # Ausdruecklich: der Supervisor hat NICHT gesucht. Wer das
            # verschweigt, laedt ein, seine Saetze fuer recherchiert zu halten.
            "searched": False,
            "content_trust": "model_output",
            "hinweis": ("Das ist der INHALT deiner Antwort. Sprich ihn in deinem "
                        "eigenen Ton aus, kuerze wenn noetig -- aber erfinde "
                        "nichts dazu. Dieser Weg hat NICHT gesucht: Quellen und "
                        "Zeitstand stammen, wenn ueberhaupt, aus dem "
                        "uebergebenen Zusammenhang."),
        }

    async def _einmal(self, question: str, context: str) -> dict:
        """Genau EIN gemaklerter Aufruf, mit eigenem Lease im `finally`."""
        broker = self.broker
        if broker is None:
            return {"ok": False, "reason": "broker_absent"}

        token = broker.register_principal(VOICE_SUPERVISOR_PRINCIPAL)
        lease_id = ""
        try:
            lease_id = broker.open_lease(VOICE_SUPERVISOR_PRINCIPAL,
                                         question[:80],
                                         deadline=time.time() + LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("voice_supervisor.lease_refused", kind=type(exc).__name__)
            return {"ok": False, "reason": getattr(exc, "reason", "lease_refused")}
        try:
            return await self._transport(_payload(question, context),
                                         token=token, port=self._port)
        except Exception as exc:  # noqa: BLE001
            log.warning("voice_supervisor.call_failed", kind=type(exc).__name__)
            return {"ok": False, "reason": "voice_supervisor_failed"}
        finally:
            if lease_id:
                try:
                    broker.close_lease(lease_id)
                except Exception as exc:  # noqa: BLE001 - nie den Core stoeren
                    log.info("voice_supervisor.lease_close_failed",
                             kind=type(exc).__name__)


def register(router: Any, capabilities: VoiceSupervisorCapabilities) -> list[str]:
    handlers = {"voice_supervisor_answer": capabilities.answer}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return list(handlers)
