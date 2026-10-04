"""Kurzrecherche als Faehigkeit -- eine aktuelle Frage, im selben Gespraechs-Turn.

Der Hermes-Deep-Pfad (`deep_research`) ist gruendlich und langsam: er wartet
`FIRST_WAIT` Sekunden und meldet sich sonst spaeter. Fuer "wer ist gerade
Bundeskanzler" ist das die falsche Antwort -- nicht falsch im Ergebnis, falsch
im Takt. Diese Faehigkeit nimmt einen anderen Weg: die NATIVE Websuche des
Anbieters fuehrt aus, in EINEM Anfrage-Antwort-Paar, und SOLVIO behaelt genau
die vier Dinge, die es bei jeder anderen Faehigkeit auch behaelt -- Auftrag
(eigener Auftraggeber, eigenes Lease), Kappe (eigene Caps, eigenes Broker-Tor
fuer das anbieterseitige Werkzeug), Vertrauen (`content_trust: untrusted_web`,
dieselbe Entwaffnungshuelle wie beim Browser) und Wahrheit (Quellen kommen
AUSSCHLIESSLICH aus den `url_citation`-Annotationen des Anbieters, nie aus
geratenen URLs).

**Der Aufrufweg folgt woertlich `cognition/assessor.py`.** Ein frischer Token
je Aufruf (`register_principal` praegt neu), ein eigenes Lease, ein POST auf
die Broker-Rueckschleife, `close_lease` in einem `finally`, das nie wirft. Kein
Hermes, keine Agentenlaufzeit, keine Hintergrundaufgabe: `execution_class` ist
FAST, `executor` ist `inline`, und der ganze Aufruf laeuft im Turn.

**Der Nebenbefund aus der Live-Abnahme (2026-09-02):** das Modell schreibt eine
Quelle ZWEIMAL -- strukturiert in den `url_citation`-Annotationen UND als
Markdown-Zitat mitten im Antworttext ("... Merz. ([bundesregierung.de]"
"(https://...))"). Genau das zweite darf nicht vorgelesen werden. Der
Antworttext wird deshalb von eingebetteten Markdown-Zitaten UND von jeder
verbliebenen nackten URL befreit, bevor er als `answer` zurueckgeht -- die
Quellen stehen strukturiert daneben, in `sources[]`.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from solvio.capabilities.browser import CONTENT_TRUST
from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilitySpec, ExecutionClass, ExecutorUnavailable,
)
from solvio.contracts.untrusted import neutralize
from solvio.logging_setup import get_logger
from solvio.provider_broker.service import RESEARCH_QUICK_PRINCIPAL
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("research_quick")

#: Das einzige Modell, das dieser Auftraggeber anfordern darf
#: (`RESEARCH_QUICK_CAPS.allowed_models`, `provider_broker/session.py`) --
#: hier referenziert, nicht abgeschrieben, wie ueberall im Haus.
MODEL = "gpt-5.4-mini"

#: Was in `sources[].domain`-Nachbarschaft und im Ergebnis steht -- eine
#: Beschriftung, kein Zugangsname.
PROVIDER = "openai"

#: Frist des einzelnen POSTs auf die Broker-Rueckschleife.
#:
#: **VORLAEUFIG.** Das Brokerbuch haelt 20 echte Aufrufe dieses
#: Auftraggebers -- Median 3,53 s, p90 4,92 s, laengster 16,93 s. Diese Zahlen
#: stammen aus dem Weg OHNE erzwungene Suche und sind damit KEINE Messung
#: dieses Kandidaten: mit `tool_choice: "required"` sucht der Anbieter in
#: jedem Aufruf, und wie lange das dauert, ist hier nicht gemessen.
#:
#: 45 s ist deshalb eine Einstellung mit Kalibrierungsauftrag, kein Ergebnis:
#: sie gibt dem laengsten bekannten Aufruf reichlich Luft. Der erste
#: Integrationstest liefert die Zahlen fuer diesen Weg -- `elapsed_ms` steht
#: in jeder Protokollzeile.
REQUEST_TIMEOUT = 45.0

#: Lease-Dauer je Aufruf. Deckt beide Versuche plus Spielraum.
LEASE_SECONDS = 120.0

#: Angeheftete Ausgabekappe.
#:
#: **VORLAEUFIG -- und die Begruendung ist schwaecher, als sie klang.**
#:
#: Gemessen ist nur: am 07.09.2026 lief eine echte Frage 16,9 s, der Umschlag
#: war 16 344 Byte gross, und es war kein Antwortteil darin. **Bytes belegen
#: keine ausgeschoepfte Tokenkappe.** Ein grosser Umschlag kann Denkspuren,
#: Sucheintraege oder Anbieter-Metadaten tragen; welcher Grund es war, ist
#: NICHT bekannt, weil damals niemand `status`, `incomplete_details` oder
#: `usage` las. Genau diese Luecke schliesst `_response_state`.
#:
#: 800 war eine Kappe, unter der ein Fehlschlag auftrat -- mehr sagt der
#: Befund nicht. 2500 ist deshalb eine EINSTELLUNG mit Kalibrierungsauftrag:
#: mehr Luft als der Wert, unter dem es schiefging, und unter der Pauschale,
#: die der Broker sonst veranschlagt (4096). Der wahre Verbrauch steht ab
#: jetzt in jeder Protokollzeile (`out_tokens`, `reasoning_tokens`) und
#: ersetzt diese Zahl, sobald er gemessen ist.
MAX_OUTPUT_TOKENS = 2500

#: Die Suche wird VERLANGT, nicht angeboten.
#:
#: Der gemessene Grund: am 07.09.2026 gab der Anbieter auf „Und wie war das bei
#: der vorherigen Landtagswahl?" in 2,4 Sekunden eine 280-Zeichen-Antwort mit
#: `gesehen=0` und `sources=0` zurueck -- kein einziger `web_search_call`. Mit
#: `tool_choice: "auto"` (dem Standard) entscheidet das Anbietermodell selbst,
#: ob es sucht, und in diesem Aufruf hat es nicht gesucht.
#:
#: **Ueber den 06.09. sagt das nichts Bewiesenes.** Der damalige Umschlag ist
#: nirgends aufgezeichnet; dass die Antworten von damals ebenfalls unrecherchiert
#: waren, ist mit dem Befund VEREINBAR, aber nicht belegt. Der Unterschied
#: zaehlt: das eine ist eine Messung, das andere eine Vermutung.
#:
#: `"required"` ist die dokumentierte Form der Anbieter-API fuer gehostete
#: Werkzeuge. Der Broker reicht den Rumpf byteweise unveraendert weiter
#: (`service.py`, `data=raw`); sein Werkzeugtor prueft `tools`, nie
#: `tool_choice`.
TOOL_CHOICE = "required"

#: Hoechstens ZWEI Versuche -- einer plus genau eine gezielte Wiederholung.
MAX_ATTEMPTS = 2

#: Und beide zusammen niemals laenger als das hier. Ein zweiter Versuch wird
#: nur begonnen, wenn danach noch eine volle Frist hineinpasst; sonst ist die
#: Wiederholung eine Frist, die der Turn nicht hat.
TOTAL_BUDGET_SECONDS = 100.0

#: Was eine Wiederholung ueberhaupt rechtfertigt: ein TECHNISCHER Fehlschlag
#: des Transports, der beim zweiten Mal anders ausgehen kann.
#:
#: Ausdruecklich NICHT hier drin: jede Kappe (`token_capped`, `rate_capped`),
#: jede Ablehnung des Brokers und jedes inhaltliche Ergebnis. Eine Antwort ohne
#: Suchnachweis ist ein ERGEBNIS, kein Fehlschlag -- sie noch einmal zu holen
#: waere Wuerfeln auf Kosten des Nutzers.
RECOVERABLE = frozenset({"broker_timeout", "broker_502", "broker_503",
                         "broker_504", "broker_unreadable"})


def _is_recoverable(reason: str) -> bool:
    """Ein technischer Aussetzer -- oder eine Antwort, die bestehen bleibt."""
    grund = str(reason or "")
    return grund in RECOVERABLE or grund.startswith("broker_unreachable")

MIN_QUESTION = 3
MAX_QUESTION = 800

#: Was das sprechende Modell mit einem BELEGTEN Ergebnis tun soll.
SOURCED_HINT = ("Nenne die Quelle beim Namen und den Stand, auf den sich die "
                "Zahlen beziehen. Keine URL vorlesen.")

#: Und was mit einem Ergebnis, das zwar eine Suche hinter sich hat, aber keine
#: Quelle nennt. Gemessen als der Fall, in dem SOLVIO frueher eine erfundene
#: Zugriffssperre erzaehlte, statt zu sagen, was wirklich fehlte.
UNSOURCED_HINT = ("Die Suche lief, hat aber keine Quelle mitgeliefert. Sage "
                  "das offen und gib den Stand als unbelegt aus. Behaupte "
                  "NICHT, du duerftest nicht auf aktuelle Quellen zugreifen.")

SPECS: dict[str, CapabilitySpec] = {
    "research_quick": CapabilitySpec(
        name="research_quick", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "question": {"type": "string"}}, "required": ["question"]},
        executor="inline", timeout=REQUEST_TIMEOUT + 5.0,
        description="Beantwortet eine aktuelle Faktenfrage sofort, im selben "
                    "Gespraech, mit der nativen Websuche des Anbieters."),
}

# -- Markdown-Zitate und nackte URLs entfernen -------------------------------

#: `([Titel](https://...))` -- die vollstaendige, in Klammern eingefasste
#: Form, wie sie in der Live-Abnahme im Antworttext auftauchte.
_MD_CITATION = re.compile(r"\(\[[^\]\n]{0,200}\]\(https?://[^\s()]+\)\)")
#: Dieselbe Form ohne die aeussere Klammer.
_MD_LINK = re.compile(r"\[[^\]\n]{0,200}\]\(https?://[^\s()]+\)")
#: Was danach noch an nackter URL uebrig sein koennte -- der Sicherheitsnetz-
#: Fall, nicht der erwartete.
_BARE_URL = re.compile(r"https?://\S+")


def _strip_embedded_urls(text: str) -> str:
    """Nimmt dem Sprechtext jede URL -- eingebettet als Markdown oder nackt."""
    cleaned = _MD_CITATION.sub("", text)
    cleaned = _MD_LINK.sub("", cleaned)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _domain(url: str) -> str:
    """Aus der URL ABGELEITET, nie geraten. Ein Parsefehler heisst: keine."""
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""


#: Der EINZIGE Status, der eine durchgelaufene Suche belegt. Eine
#: POSITIVLISTE, und der Unterschied ist kein Geschmack.
#:
#: **Hier stand vorher eine Negativliste** -- „alles ausser failed, incomplete,
#: cancelled, in_progress zaehlt als gelaufen". Der Chief Architect hat sie
#: mit drei Umschlaegen zerlegt, und einer davon genuegt zum Verstehen:
#: `status="invented_status"` wurde als erfolgreiche Suche gebucht. Eine
#: Negativliste muss jeden Fehlerfall im Voraus kennen; ein Anbieter, der
#: morgen einen neuen Status einfuehrt, faellt automatisch auf die gute Seite.
#: Das ist die falsche Richtung fuer einen BELEG.
#:
#: `in_progress` und `searching` sind keine Fehler, sondern Zwischenstaende --
#: und genau deshalb auch kein Nachweis: eine Suche, die noch laeuft, hat
#: nichts gefunden.
SEARCH_COMPLETED = frozenset({"completed"})

#: Statuswerte, von denen wir wissen, dass sie ein Fehlschlag oder ein
#: Zwischenstand DIESES EINTRAGS sind. Sie aendern NICHTS an der Entscheidung
#: -- die faellt allein an `SEARCH_COMPLETED`. Sie dienen der ehrlichen
#: Meldung: „die Suche kam nicht durch" ist etwas anderes als „ich kann es
#: nicht feststellen", und der Mensch hat ein Recht auf die richtige.
#:
#: **Ohne `incomplete` und `cancelled`, und das ist eine Korrektur.** Beide
#: standen in der alten Negativliste und wurden von dort uebernommen, ohne
#: geprueft zu werden. Sie gehoeren zum Zustand der GESAMTEN Antwort, nicht zu
#: dem eines Sucheintrags. Ein geratener Wert in einer Liste, die eine Meldung
#: praezisiert, macht die Meldung nicht praeziser -- er macht sie erfunden.
#: Tauchen sie an einem Eintrag doch auf, fallen sie auf
#: `search_state_unknown`, und das ist die ehrliche Antwort.
SEARCH_UNFINISHED = frozenset({"failed", "in_progress", "searching"})

#: Zustaende der GESAMTEN Antwort, die einen Abbruch AUSDRUECKLICH melden.
#:
#: **Warum das eine eigene Pruefung braucht.** Ein Umschlag mit
#: abgeschlossener Suche, Quellenannotation und dem Text „Der aktuelle Stand
#: lautet: Partei A liegt bei" kam als voller Erfolg durch — bei
#: `status=incomplete` mit `reason=max_output_tokens` ebenso wie bei
#: `status=failed`. Die Stimme haette den Satz mitten im Wort abgebrochen
#: vorgelesen, mit Quelle daneben, als waere er fertig. Vorhandener Text ist
#: kein Beleg dafuer, dass die Antwort zu Ende ging.
#:
#: **Dieselbe Regel wie beim Suchbeleg, nur in die andere Richtung.** Dort
#: zaehlt nur ein AUSDRUECKLICH gemeldeter Erfolg; hier blockiert nur ein
#: AUSDRUECKLICH gemeldeter Abbruch. Beide Male gilt: aus Abwesenheit wird
#: nichts geschlossen. Ein fehlender Zustand ist kein Erfolgsbeleg — und
#: ebenso wenig ein Abbruch.
#: **Und die Pruefung ist DREIWERTIG.** Ein unabhaengiger Angriff hat gemessen,
#: dass eine blosse Abbruchliste nur halb schliesst: von 38 gestellten
#: Umschlaegen mit demselben halben Satz kamen **32 als voller Erfolg durch**,
#: sobald der Zustand ausserhalb dieser Menge lag. `in_progress` und `queued`
#: sind reale Zustaende der Anbieter-API und heissen woertlich „nicht fertig";
#: ein erfundener Wert faellt genauso auf die gute Seite. Das ist derselbe
#: Fehler wie eine Negativliste, nur in Positivform aufgeschrieben.
#:
#: Deshalb: `completed` ist Erfolg, diese Menge ist Abbruch, und **jeder andere
#: gemeldete Wert ist ein dritter Fall**. Ein FEHLENDER Zustand bleibt
#: durchlaessig -- aus Abwesenheit wird nichts geschlossen, so wie sie beim
#: Suchbeleg kein Erfolgsbeleg ist.
RESPONSE_ABORTED = frozenset({"incomplete", "failed", "cancelled"})

#: Der eine Zustand, der eine regulaer zu Ende gelaufene Antwort meldet.
RESPONSE_COMPLETED = frozenset({"completed"})

#: Welcher `content`-Teil die ANTWORT ist. Wieder eine Positivliste, und aus
#: demselben Grund.
#:
#: **Vorher wurde der Teiltyp nie angesehen** -- genommen wurde der erste Teil
#: mit einem `text`-Schluessel. Ein unabhaengiger Angriff hat gemessen, was das
#: heisst: ein `reasoning_text`-Teil, in dem das Modell woertlich schreibt „ich
#: habe nichts gefunden, ich nehme meinen Trainingsstand", wurde als
#: recherchierte Antwort ausgegeben. SOLVIO haette die privaten Ueberlegungen
#: eines Modells als eigene Auskunft vorgelesen -- und ausgerechnet die eine,
#: die das Gegenteil dessen sagt, was gesprochen wurde.
ANSWER_PARTS = frozenset({"output_text"})


def _answer_item(data: Any) -> tuple[dict[str, Any] | None, str]:
    """Welcher Eintrag IST die Antwort — strukturell, nicht nach Worten.

    **Der Befund.** Traegt der Umschlag mehrere Nachrichten, gewann bisher die
    ERSTE mit Text. Gemessen an einer Vorbemerkung vor dem Suchaufruf und dem
    Ergebnis danach: gesprochen wurde „Einen Moment, ich sehe kurz nach." --
    mit Quelle, Zeitstand und `searches=1` daneben, also mit dem vollen
    Anschein einer belegten Recherche.

    **Die Regel folgt der Reihenfolge des Anbieters, nicht dem Wortlaut.** Die
    Anbieterdokumentation ordnet den Umschlag eindeutig: der
    `web_search_call`-Eintrag steht VOR der Assistentennachricht mit der
    Antwort, und die Quellen liegen IN dieser Nachricht
    (`message.content[].annotations`). Eine Vorbemerkung kann deshalb nie nach
    der Suche stehen, auf die sie vorbereitet.

    Die Antwort ist also: die LETZTE Assistentennachricht, die NACH dem letzten
    Suchaufruf steht. Kein „letzter Text gewinnt" -- eine Nachricht vor der
    Suche kommt gar nicht in Frage --, und keine Wortheuristik: es wird kein
    einziges Wort angesehen.

    Gibt es keine solche Nachricht, gibt es keine Antwort. Eine Vorbemerkung
    allein ist kein Ergebnis.
    """
    if not isinstance(data, dict):
        return None, ""
    eintraege = [e for e in (data.get("output") or []) if isinstance(e, dict)]
    letzte_suche = -1
    for index, eintrag in enumerate(eintraege):
        if eintrag.get("type") == "web_search_call":
            letzte_suche = index
    gewaehlt: dict[str, Any] | None = None
    for index, eintrag in enumerate(eintraege):
        if index <= letzte_suche:
            continue
        if eintrag.get("type") not in (None, "message"):
            continue
        rolle = eintrag.get("role")
        # Ohne Rollenangabe wird nichts angenommen; eine FREMDE Rolle wird
        # ausgeschlossen. Der Anbieter fuehrt `role: "assistant"`.
        if isinstance(rolle, str) and rolle.strip().lower() != "assistant":
            continue
        gewaehlt = eintrag
    if gewaehlt is None:
        return None, ""
    roh = gewaehlt.get("status")
    status = roh.strip().lower()[:40] if isinstance(roh, str) else ""
    return gewaehlt, status


def _extract_answer_and_citations(
        data: Any) -> tuple[str, list[dict[str, str]], dict[str, int], str]:
    """Der Sprechtext, die Quellen -- und ob wirklich gesucht wurde.

    Quellen kommen AUSSCHLIESSLICH aus `url_citation`. Liefert der Anbieter
    keine Annotation, bleibt die Liste leer -- es wird NIE eine URL aus dem
    Text herauskonstruiert.

    **Der dritte Rueckgabewert ist neu, und er ist der Punkt.** Bis hierher
    sprang diese Schleife ueber jeden Nicht-`message`-Eintrag hinweg -- und
    damit ueber `web_search_call`, den EINZIGEN strukturellen Hinweis des
    Anbieters darauf, dass eine Suche stattgefunden hat. Ohne ihn war jede
    Antwort gleich: der reine Modelltext aus dem Vorwissen sah exakt so aus
    wie ein frisch recherchierter Stand.

    Gemessen am 06.09.2026: zwei Aufrufe, beide `outcome=success`, und die
    gesprochene Antwort nannte weder Quelle noch Zeitstand. Ob ueberhaupt
    gesucht wurde, laesst sich im Nachhinein nicht mehr feststellen -- die
    Spur existierte nicht.

    **Der dritte Rueckgabewert ist eine Zaehlung, keine Wertung.** Drei Zahlen:
    `gesehen` (wie viele Sucheintraege ueberhaupt da waren), `belegt` (wie
    viele davon `completed` sind) und `offen` (wie viele einen bekannten
    Fehl- oder Zwischenstand tragen). Entschieden wird spaeter und
    ausschliesslich an `belegt`; die anderen beiden tragen die ehrliche
    Meldung.

    **Ein Eintrag OHNE Status zaehlt NICHT als Beleg.** Diese Zeile stand
    hier vorher umgekehrt, und die Begruendung war falsch: sie berief sich auf
    eine Umschlagsform des Anbieters, die in diesem Repository nirgends belegt
    ist. Die statuslose Form stammt aus einer selbstgeschriebenen
    Testattrappe, nicht aus einer gemessenen Anbieterantwort -- ein Beleg, den
    wir uns selbst geschrieben hatten. Fehlende Angaben sind kein Nachweis.
    """
    text = ""
    citations: list[dict[str, str]] = []
    suchen = {"gesehen": 0, "belegt": 0, "offen": 0}
    gesehene_teile: set[str] = set()
    kennungen: set[str] = set()
    antwort_eintrag, antwort_status = _answer_item(data)
    if not isinstance(data, dict):
        return text, citations, suchen, antwort_status
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "web_search_call":
            # **Derselbe Eintrag zweimal ist ein Fund, kein zweiter Beleg** --
            # woertlich die Regel, die `_dedupe_sources` fuer URLs anwendet.
            # Ohne sie liesse sich der Zaehler beliebig aufblasen: 25 Kopien
            # desselben `id` ergaben 25 Suchen. Die ENTSCHEIDUNG haengt zwar
            # nur an „mindestens eine", aber eine gemeldete Zahl, die niemand
            # gezaehlt hat, ist eine erfundene Zahl.
            kennung = item.get("id")
            if isinstance(kennung, str) and kennung:
                if kennung in kennungen:
                    continue
                kennungen.add(kennung)
            roh = item.get("status")
            status = str(roh).strip().lower() if isinstance(roh, str) else ""
            suchen["gesehen"] += 1
            # Ein Eintrag, der einen Fehler mitfuehrt, ist kein positiver
            # Beleg -- auch wenn daneben `completed` steht. Diese Pruefung kann
            # die Regel nur STRENGER machen, nie durchlaessiger.
            fehler = item.get("error")
            if fehler:
                suchen["offen"] += 1
            elif status in SEARCH_COMPLETED:
                suchen["belegt"] += 1
            elif status in SEARCH_UNFINISHED:
                suchen["offen"] += 1
            continue
        if kind not in (None, "message"):
            continue
        # **Nur die AUSGEWAEHLTE Nachricht liefert Text und Quellen.** Beides
        # gehoert zusammen: Quellen aus einer Vorbemerkung an eine Antwort zu
        # heften waere derselbe falsche Anschein, nur andersherum.
        if item is not antwort_eintrag:
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            teil = part.get("type")
            if isinstance(teil, str) and teil:
                gesehene_teile.add(teil)
            part_text = part.get("text")
            if (teil in ANSWER_PARTS
                    and isinstance(part_text, str) and part_text.strip()):
                # Mehrere Textteile EINER Nachricht gehoeren zusammen.
                text = f"{text}\n{part_text}" if text else part_text
            for annotation in part.get("annotations", []) or []:
                if not isinstance(annotation, dict):
                    continue
                if annotation.get("type") != "url_citation":
                    continue
                url = str(annotation.get("url") or "").strip()
                if not url:
                    continue
                citations.append({"title": str(annotation.get("title") or "").strip(),
                                  "url": url})
    if not text and gesehene_teile:
        # Diagnose, kein Text: der Anbietervertrag ist hier nicht gemessen
        # (DEBT-0247). Taucht ein unbekannter Teiltyp auf, sagt genau diese
        # Zeile es beim ersten echten Lauf -- statt dass SOLVIO still
        # irgendeinen Text spricht.
        log.info("research_quick.no_answer_part",
                 parts=",".join(sorted(gesehene_teile))[:120])
    return text, citations, suchen, antwort_status


def _response_state(data: Any) -> dict[str, Any]:
    """Zustand, Abbruchgrund und Verbrauch -- aus der Antwort GELESEN.

    **Warum das fehlte, und was es gekostet hat.** Am 07.09.2026 lief eine
    Frage 16,9 s, der Umschlag war 16 344 Byte gross, und heraus kam
    `no_answer`. Warum, wusste niemand: diese Datei las nur `output`, und der
    Broker bucht auf dieser Strecke ausnahmslos `tokens_source=estimated` mit
    `output_tokens=0` -- 20 von 20 echten Aufrufen. Es gab also KEINE Stelle
    im ganzen Haus, die den wahren Verbrauch oder einen Abbruchgrund kannte.

    Gelesen wird nur Struktur: Zustand, Grund, Zahlen. Kein Anbietertext, kein
    Denkprotokoll, kein Zugang -- die gehen in kein Log.
    """
    zustand: dict[str, Any] = {"status": "", "incomplete": "",
                               "incomplete_reported": False,
                               "error_reported": False,
                               "message_unfinished": "",
                               "in_tokens": 0, "out_tokens": 0,
                               "reasoning_tokens": 0}
    if not isinstance(data, dict):
        return zustand

    # **Auch unter `response`.** Der eigene Broker kennt diese Form bereits und
    # liest `usage` genau dort (`provider_broker/proxy.py`: „`usage` liegt je
    # nach Pfad am Ereignis selbst oder unter `response`"). Wer nur die flache
    # Ebene liest, uebersieht denselben Bericht eine Ebene tiefer.
    verschachtelt = data.get("response")
    if isinstance(verschachtelt, dict):
        for schluessel in ("status", "incomplete_details", "usage", "error"):
            if schluessel not in data and schluessel in verschachtelt:
                data = {**data, schluessel: verschachtelt[schluessel]}

    # Ein Fehlerobjekt auf ANTWORTEBENE ist ein gemeldeter Fehler -- auch neben
    # `status="completed"`. Geprueft wird die ANWESENHEIT, nicht der Inhalt.
    zustand["error_reported"] = data.get("error") is not None

    # **Die Anwesenheit ist das Signal.** Bei einer sauberen Antwort steht hier
    # `null`; steht ueberhaupt etwas, hat der Anbieter einen Abbruch gemeldet.
    # Der `reason` praezisiert ihn nur -- ihn zu verlangen hiess, ein leeres
    # oder anders geformtes `incomplete_details` durchzulassen (gemessen:
    # `{}`, `{"reason": null}`, ein String, eine Liste).
    zustand["incomplete_reported"] = data.get("incomplete_details") is not None

    # Der Zustand des Antworteintrags wird NICHT hier bestimmt: welcher
    # Eintrag die Antwort ist, entscheidet `_answer_item` — und nur dessen
    # Zustand zaehlt. Eine unfertige Vorbemerkung sagt nichts ueber die
    # Antwort, die danach kam. `research()` traegt ihn nach.
    status = data.get("status")
    if isinstance(status, str):
        zustand["status"] = status.strip().lower()[:40]
    details = data.get("incomplete_details")
    if isinstance(details, dict):
        grund = details.get("reason")
        if isinstance(grund, str):
            zustand["incomplete"] = grund.strip().lower()[:40]
    usage = data.get("usage")
    if isinstance(usage, dict):
        for feld, ziel in (("input_tokens", "in_tokens"),
                           ("output_tokens", "out_tokens")):
            wert = usage.get(feld)
            if isinstance(wert, int):
                zustand[ziel] = max(0, wert)
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            wert = details.get("reasoning_tokens")
            if isinstance(wert, int):
                zustand["reasoning_tokens"] = max(0, wert)
    return zustand


def _dedupe_sources(citations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dieselbe URL zweimal ist ein Fund des Anbieters, kein zweiter Beleg."""
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for citation in citations:
        url = citation["url"]
        if url not in merged:
            merged[url] = {"title": citation.get("title", ""), "url": url,
                           "domain": _domain(url)}
            order.append(url)
        elif not merged[url]["title"] and citation.get("title"):
            merged[url]["title"] = citation["title"]
    return [merged[url] for url in order]


# -- Der Transport: woertlich der Weg aus cognition/assessor.py -------------

#: Was der Anbieter ausser der Frage noch wissen muss. Ohne das Datum ist
#: „heute" fuer ein Modell unbestimmt: gemessen am 06.09.2026 fragte der
#: Mensch nach „den Wahlen, die heute in Sachsen-Anhalt sind", und die Antwort
#: nannte Oeffnungszeiten von Wahllokalen statt eines Standes. Ein Datum ist
#: keine themenspezifische Wortregel -- es ist die Tatsache, gegen die
#: „heute", „gerade" und „aktuell" ueberhaupt erst etwas bedeuten.
SEARCH_INSTRUCTION = (
    "Heute ist {datum}. Suche im Web, bevor du antwortest. Nenne den Stand, "
    "auf den sich deine Zahlen beziehen, mit Datum und Uhrzeit. Hast du keinen "
    "belastbaren aktuellen Stand gefunden, sage genau das."
)


def _today(now: float = 0.0) -> str:
    """Das heutige Datum, ISO, in Ortszeit. Eine Uhr, die ein Test ersetzt."""
    moment = datetime.fromtimestamp(now or time.time())
    return moment.strftime("%Y-%m-%d %H:%M")


def _payload(question: str, *, now: float = 0.0) -> dict[str, Any]:
    return {
        "model": MODEL,
        "input": [
            {"role": "system",
             "content": SEARCH_INSTRUCTION.format(datum=_today(now))},
            {"role": "user", "content": question},
        ],
        "tools": [{"type": "web_search", "search_context_size": "low"}],
        # Die Suche wird VERLANGT. Ohne diese Zeile entscheidet das
        # Anbietermodell selbst, ob es nachsieht -- und am 07.09.2026 hat es
        # sich dagegen entschieden.
        "tool_choice": TOOL_CHOICE,
        # NICHT "minimal" -- die Anbieterdoku schliesst das fuer diesen Pfad
        # ausdruecklich aus (Live-Abnahme 2026-09-02).
        "reasoning": {"effort": "low"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def _denied_reason(body: str) -> str:
    """`{"error": {"type": "solvio_broker", "code": "<grund>"}}` -- oder nichts.

    Dieselbe Lesart wie in `cognition/assessor.py`: der Grund des Brokers wird
    GELESEN, nicht geraten.
    """
    try:
        parsed = json.loads(body or "")
    except ValueError:
        return ""
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code", "") or "")


async def _post_broker(payload: dict, *, token: str, port: int = 0,
                       timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST an den Broker. Ein Anbieterfehler ist keine Schema-Frage.

    Woertlich derselbe Weg wie `cognition.assessor.broker_transport` -- nur
    dass hier der GANZE gepufferte Rumpf zurueckgeht, weil die Quellen aus
    `output[].content[].annotations` stammen und nicht nur aus dem Text.
    """
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
                    log.warning("research_quick.rejected", status=response.status,
                               reason=reason)
                    return {"ok": False, "reason": reason}
                try:
                    data = json.loads(body)
                except ValueError:
                    return {"ok": False, "reason": "broker_unreadable"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "reason": f"broker_unreachable:{type(exc).__name__}"}
    except TimeoutError:
        return {"ok": False, "reason": "broker_timeout"}
    return {"ok": True, "data": data}


async def _call(question: str, *, broker: Any, transport: Any, port: int,
                now: float = 0.0) -> dict:
    """Genau ein gemaklerter Aufruf, mit eigenem Lease im `finally`.

    Woertlich der Weg aus `cognition.assessor.Assessor.call`: frischer Token
    je Aufruf, eigenes Lease, POST, `close_lease` in einem `finally`, das nie
    wirft.
    """
    if broker is None:
        return {"ok": False, "reason": "broker_absent"}

    token = broker.register_principal(RESEARCH_QUICK_PRINCIPAL)
    lease_id = ""
    try:
        lease_id = broker.open_lease(RESEARCH_QUICK_PRINCIPAL, question[:80],
                                     deadline=time.time() + LEASE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
        log.info("research_quick.lease_refused", kind=type(exc).__name__)
        return {"ok": False, "reason": getattr(exc, "reason", "lease_refused")}
    try:
        return await transport(_payload(question, now=now), token=token, port=port)
    except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist keine Erlaubnis
        log.warning("research_quick.call_failed", kind=type(exc).__name__)
        return {"ok": False, "reason": "research_quick_failed"}
    finally:
        # Steht in einem `finally` und darf deshalb nie werfen -- dieselbe
        # Regel wie beim Broker selbst.
        if lease_id:
            try:
                broker.close_lease(lease_id)
            except Exception as exc:  # noqa: BLE001 - nie den Core stoeren
                log.info("research_quick.lease_close_failed",
                         kind=type(exc).__name__)


class ResearchQuickCapabilities:
    """Der Handler. Der Broker wird SPAET gelesen, nie beim Anhaengen gemerkt.

    Derselbe Grund wie beim kognitiven Router: der Broker haengt am Server,
    nicht am Dispatcher, und entsteht NACH dieser Registrierung. Ein einmal
    gemerktes `None` waere fuer die gesamte Laufzeit `broker_absent`.
    """

    def __init__(self, dispatcher: Any, *, transport: Any = None,
                port: int = 0) -> None:
        self.dispatcher = dispatcher
        self._transport = transport if transport is not None else _post_broker
        self._port = port

    @property
    def broker(self) -> Any:
        return getattr(self.dispatcher, "provider_broker", None)

    async def research(self, arguments: dict[str, Any], *,
                       now: float = 0.0) -> dict[str, Any]:
        question = str(arguments.get("question", "") or "").strip()
        if len(question) < MIN_QUESTION:
            raise CapabilityDeclined("question_too_short",
                                     "Wonach genau soll ich kurz suchen?")
        if len(question) > MAX_QUESTION:
            raise CapabilityDeclined("question_too_long",
                                     "Das ist zu lang -- bitte kuerzer fassen.")

        # **Hoechstens ein zweiter Versuch, und nur bei einem technischen
        # Aussetzer.** Nicht bei einer Kappe, nicht bei einer Ablehnung, und
        # niemals bei einem inhaltlichen Ergebnis -- eine Antwort ohne
        # Suchnachweis wird nicht so lange neu gewuerfelt, bis sie passt.
        # **Genau EIN Entscheider.** Vorher stand die Grenze zweimal da -- in
        # der Schleifenbedingung und in einem Abbruch darin. Eine Mutation der
        # ersten blieb dadurch wirkungslos, und eine Zusicherung, die eine
        # absichtlich eingebaute Luecke nicht bemerkt, ist keine.
        begonnen = time.monotonic()
        versuche = 0
        outcome: dict[str, Any] = {}
        while True:
            versuche += 1
            outcome = await _call(question, broker=self.broker,
                                  transport=self._transport, port=self._port,
                                  now=now)
            if outcome.get("ok"):
                break
            grund = str(outcome.get("reason") or "")
            if not _is_recoverable(grund):
                break
            if versuche >= MAX_ATTEMPTS:
                break
            # Ein zweiter Versuch wird nur begonnen, wenn danach noch eine
            # VOLLE Frist hineinpasst. Sonst ist die Wiederholung eine Frist,
            # die der Turn nicht hat.
            verbraucht = time.monotonic() - begonnen
            if verbraucht + REQUEST_TIMEOUT > TOTAL_BUDGET_SECONDS:
                log.info("research_quick.retry_skipped", reason=grund,
                         elapsed_ms=int(verbraucht * 1000))
                break
            log.info("research_quick.retry", reason=grund, attempt=versuche,
                     elapsed_ms=int(verbraucht * 1000))

        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or "research_quick_failed")
            log.info("research_quick.transport_failed", reason=reason,
                     attempts=versuche,
                     elapsed_ms=int((time.monotonic() - begonnen) * 1000))
            error = ExecutorUnavailable(reason)
            # `CapabilityDeclined` traegt seinen maschinenlesbaren Grund seit
            # jeher als Attribut; `ExecutorUnavailable` ist im gemeinsamen
            # Vertrag absichtlich duenner. Dieser Pfad braucht den Namen aber
            # fuer die kurze, ehrliche Sprachmeldung und darf ihn nicht aus
            # Freitext zurueckraten.
            error.reason = reason
            raise error

        text, citations, suchen, antwort_status = _extract_answer_and_citations(
            outcome.get("data"))
        zustand = _response_state(outcome.get("data"))
        # Der Zustand, der zaehlt, ist der der AUSGEWAEHLTEN Antwort. Vorher
        # blockierte jede unfertige Nachricht im Umschlag -- auch eine
        # Vorbemerkung, die gar nicht gesprochen wird.
        zustand["message_unfinished"] = antwort_status if (
            antwort_status and antwort_status not in RESPONSE_COMPLETED) else ""
        # **Auf JEDEM Pfad, nicht nur beim Erfolg.** Genau dieser blinde Fleck
        # liess den 16-KB-Umschlag vom 07.09. unerklaert: der `no_answer`-Pfad
        # protokollierte gar nichts, und niemand konnte sagen, ob gesucht
        # wurde, ob die Antwort abbrach oder wie viel sie verbraucht hatte.
        log.info("research_quick.response", attempts=versuche,
                 elapsed_ms=int((time.monotonic() - begonnen) * 1000),
                 answer_chars=len(text), sources=len(citations), **suchen,
                 **zustand)
        # **Zuerst der Zustand der GANZEN Antwort, unabhaengig vom Text.**
        # Diese Reihenfolge ist der Kern der Korrektur: vorher stand die
        # Abbruchpruefung INNERHALB des „kein Text"-Zweigs und griff deshalb
        # genau dann nicht, wenn es darauf ankam — bei einem halben Satz.
        if zustand["incomplete_reported"] or zustand["message_unfinished"]:
            raise CapabilityDeclined(
                "answer_truncated",
                "Die Antwort ist abgebrochen, bevor sie fertig war — ich gebe "
                "dir kein halbes Ergebnis als ganzes aus.")
        if zustand["error_reported"] or zustand["status"] in RESPONSE_ABORTED:
            raise CapabilityDeclined(
                "answer_aborted",
                "Die Anfrage ist beim Anbieter nicht sauber zu Ende gelaufen "
                "— was da steht, ist nicht vollstaendig.")
        if zustand["status"] and zustand["status"] not in RESPONSE_COMPLETED:
            # Weder Erfolg noch gemeldeter Abbruch: `in_progress`, `queued`
            # oder etwas, das wir nicht kennen. Der halbe Satz mit Quelle
            # daneben faellt genau hierher — und nicht auf die gute Seite.
            raise CapabilityDeclined(
                "answer_state_unknown",
                "Ich kann nicht feststellen, ob die Antwort fertig ist — "
                "deshalb gebe ich sie dir nicht als Ergebnis aus.")
        if not text.strip():
            raise CapabilityDeclined("no_answer",
                                     "Dazu habe ich gerade keine Antwort bekommen.")

        answer = neutralize(_strip_embedded_urls(text))
        sources = _dedupe_sources(citations)
        for source in sources:
            source["title"] = neutralize(source["title"], limit=300)

        # **Ein technisch gelungener Aufruf ist keine beantwortete Frage.**
        # Ohne nachgewiesene Suche ist das hier das Vorwissen eines
        # Sprachmodells -- moeglicherweise richtig, moeglicherweise ein Jahr
        # alt, in jedem Fall unbelegt.
        #
        # **Drei Dinge, die getrennt bleiben muessen.** Vorher stand hier
        # `if not searches and not sources` -- ein ODER, und damit durfte eine
        # Quellenannotation eine fehlende oder gescheiterte Suche ersetzen. Der
        # Chief Architect hat genau das reproduziert: `suchen=0` plus eine
        # Annotation kam als belegte Recherche durch, und `status="failed"`
        # plus Annotation ebenso.
        #
        # Eine Annotation ist aber kein Nachweis einer Ausfuehrung. Sie steht
        # im Text, den dasselbe Modell geschrieben hat; sie sagt „ich beziehe
        # mich hierauf", nicht „ich habe eben nachgesehen". Deshalb:
        #
        #   (a) AUSFUEHRUNG  -- `suchen["belegt"]`, allein entscheidend
        #   (b) QUELLE       -- `sources`, faerbt nur die Meldung
        #   (c) INHALT       -- `text`, schon oben geprueft
        #
        # Und gemischt heisst nicht schlechter: ein gescheiterter Versuch
        # entwertet einen nachweislich durchgelaufenen nicht. Ein `completed`
        # genuegt.
        # **Drei Gruende, nicht einer.** „Es gab keine Suche", „die Suche kam
        # nicht durch" und „ich kann nicht feststellen, ob sie durchlief" sind
        # drei verschiedene Wahrheiten, und der Mensch hat ein Recht auf die
        # richtige. Der dritte ist ausserdem das Diagnosemittel, das dieser
        # Datei fehlt: der tatsaechliche Umschlag des Anbieters ist in diesem
        # Repository nirgends gemessen (DEBT-0247). Taucht ein unbekannter
        # Statuswert auf, sagt genau dieser Grund es beim ersten echten Lauf --
        # statt still auf die gute Seite zu fallen, wie es die alte
        # Negativliste tat.
        if not suchen["belegt"]:
            log.info("research_quick.unproven", chars=len(answer),
                     sources=len(sources), **suchen)
            if not suchen["gesehen"]:
                raise CapabilityDeclined(
                    "no_search_evidence",
                    "Ich habe dazu keine Suche zustande gebracht — was ich "
                    "sagen koennte, waere nur mein eigener Wissensstand ohne "
                    "Quelle.")
            if suchen["offen"] == suchen["gesehen"]:
                raise CapabilityDeclined(
                    "search_not_completed",
                    "Die Suche kam nicht durch — ich habe dazu keinen "
                    "belegten aktuellen Stand.")
            raise CapabilityDeclined(
                "search_state_unknown",
                "Ich kann nicht feststellen, ob die Suche wirklich durchlief "
                "— deshalb gebe ich dir dazu keine Zahl als recherchiert aus.")

        log.info("research_quick.answered", sources=len(sources),
                 chars=len(answer), attempts=versuche,
                 elapsed_ms=int((time.monotonic() - begonnen) * 1000), **suchen)
        return {
            "answer": answer,
            "sources": sources,
            "searches": suchen["belegt"],
            "provider": PROVIDER,
            "model": MODEL,
            "searched_at": datetime.now(timezone.utc).isoformat(),
            # Derselbe Zeitpunkt, aber sprechbar. `searched_at` ist UTC und
            # maschinengenau; wer ihn vorliest, sagt etwas Richtiges auf eine
            # Art, die niemand versteht. Der Sprachpfad bekommt beides.
            "gesucht_um": _today(now),
            "content_trust": CONTENT_TRUST,
            # Ein Hinweis an das sprechende Modell, kein Zwang: der Core kann
            # verlangen, dass Quelle und Zeitstand vorkommen, und die Daten
            # danebenlegen -- durchsetzen kann er es nicht.
            "hinweis": (SOURCED_HINT if sources else UNSOURCED_HINT),
        }


def register(router: Any, capabilities: ResearchQuickCapabilities) -> list[str]:
    handlers = {"research_quick": capabilities.research}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
