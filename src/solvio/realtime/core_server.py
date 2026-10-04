"""SOLVIO Core - persistenter Server mit Session-on-Demand (Schritt 15).

Haelt einen WebSocket-Server offen. Der Satellit verbindet sich ERST nach einem
lokalen Wake-Word und schickt 'session_start'. Erst dann wird eine OpenAI-
Realtime-Session geoeffnet. Bei 30 s echter Gespraechsinaktivitaet (niemand
spricht, SOLVIO spricht nicht, keine laufende Antwort) wird die Session sauber
geschlossen und der Satellit ('session_end') zurueck in IDLE geschickt.

Im IDLE existiert KEINE OpenAI-Verbindung und es geht KEIN Raumaudio zu OpenAI.
"""
from __future__ import annotations

import asyncio
from collections import deque
import base64
import binascii
import json
import os
import re
import secrets
import signal
import socket
import sys
import time
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

from solvio.audio.resample import Resampler
from solvio.conversation import ConversationStore, ConversationStoreError


class ConversationBindingError(ConversationStoreError):
    """Ein VERLANGTER Chat liess sich nicht binden (N8/C3, §7).

    Anders als ein Speicherfehler beim Linger wird das nicht abgefangen und
    degradiert: der Oeffnungsweg endet damit, die Sitzung wird `refused`, und
    der Endpunkt schliesst den Socket mit 4401 — die App darf keine ungebundene
    Sitzung als Ersatz bekommen.
    """
from solvio.capabilities.approver_runtime import from_environment as approver_from_env
from solvio.capabilities.deep import TASK_TIMEOUT as DEEP_TASK_TIMEOUT
from solvio.deep.followup import DeepFollowUp
from solvio.deep.service import from_environment as deep_from_env
from solvio.tools.registry import attach_deep_runtime
from solvio.capabilities.invocation import voice_trust
from solvio.capabilities.policy import OriginClass, origin_for_session
from solvio.conversation.store import ROLE_ASSISTANT, ROLE_USER
from solvio.memory import intent as memory_intent
from solvio.logging_setup import get_logger
from solvio.realtime import satellite_auth as SA
from solvio.realtime.satellite_health import (HEALTH_PATH, SatelliteHealthRegistry,
                                              parse_report)
from solvio.realtime.preroll import Preroll
from solvio.realtime.voice_session import (
    OPENAI_RATE, SATELLITE_RATE, SYSTEM_INSTRUCTIONS,
)

log = get_logger("core")

#: Wie lange das Nachlesen offener Anrufe den Start hoechstens aufhalten darf.
#:
#: Zwanzig Sekunden ist die Frist EINES Anbieteraufrufs; die Wiederaufnahme
#: arbeitet die offenen Zeilen nacheinander ab. Ohne Gesamtbudget waechst die
#: Startverzoegerung mit jeder Zeile, die der Anbieter nie aufloest — und
#: solange schweigt SOLVIO. Dreissig Sekunden reichen fuer den gewoehnlichen
#: Fall (eine offene Zeile, ein erreichbarer Anbieter) und decken den
#: pathologischen ab.
TELEPHONY_RECOVERY_BUDGET_SECS = 30.0
REALTIME_URL = "wss://api.openai.com/v1/realtime"
#: Wie oft nach einem Abriss der Anbieterverbindung neu versucht wird.
#:
#: Zwei, dann ein ehrliches Ende. Ein Anbieter, der zweimal hintereinander nicht
#: antwortet, ist nicht in der dritten Sekunde wieder da — und der Mensch steht
#: derweil vor einem stummen Geraet.
RECONNECT_ATTEMPTS = 2

#: Wartezeit vor dem n-ten Versuch. Waechst, damit ein kurzer Aussetzer schnell
#: aufgefangen wird und eine echte Stoerung nicht gehaemmert wird.
RECONNECT_BACKOFF = 0.4

#: Wie lange ein Werkzeugaufruf hoechstens auf das fertige Nutzertranskript
#: wartet, bevor die Freigabepolitik entscheidet.
#:
#: Gemessen an echten Turns liegt das Transkript nach gut einer halben Sekunde
#: vor; im live gefundenen Fall lag eine Sekunde zwischen Werkzeugaufruf und
#: Transkript. Anderthalb Sekunden decken das und bleiben eine Grenze; laeuft
#: sie ab, gilt „kein belegter Auftrag" und die Handlung nimmt den Freigabeweg.
#:
#: Gewartet wird ausserdem nur, wenn in diesem Turn ueberhaupt gesprochen wurde
#: — siehe `_await_turn_text`.
TURN_TEXT_WAIT_SECONDS = 1.5

#: Das Transkriptionsmodell der Sprachsitzung. UNVERAENDERT.
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

#: Die Sprache, in der der Owner spricht.
#:
#: **Warum das hier steht.** Am 07.09.2026 wurde „Und gestern?" zweimal falsch
#: erkannt — einmal als „Hejzolvio?", einmal als **„Geçen?"**, tuerkisch. Die
#: Sitzungskonfiguration sendete bis dahin GAR KEINE Sprachangabe; das Modell
#: raet sie je Aeusserung, und bei einer Ein-Sekunden-Aeusserung raet es
#: schlecht. Kein Fehlerfall: die Erkennung meldete Erfolg und lieferte etwas
#: Falsches.
#:
#: **Der Feldname, belegt und mit seiner Grenze.** Die Anbieterdoku sagt
#: woertlich, `gpt-live-transcribe` benutze `languages` „instead of the
#: singular `language` field". Der Singular ist damit die Form der uebrigen
#: Modelle, und `gpt-4o-mini-transcribe` ist eines davon. Ausdruecklich fuer
#: dieses Modell dokumentiert ist er NICHT — das ist eine Ableitung aus der
#: Abgrenzung, keine Messung.
#:
#: **Erste Pruefung im Folgetest:** nimmt die Sitzung das Feld an? Eine
#: Ablehnung erschiene sofort als `core.openai_error` beim Sitzungsaufbau.
#: Dass die Angabe die beiden Fehler wirklich verhindert, bleibt bis zum
#: Sprachtest unbewiesen.
TRANSCRIPTION_LANGUAGE = "de"

#: Das einzige Werkzeug, das der Core selbst bedient.
END_CONVERSATION_TOOL = {
    "type": "function",
    "name": "end_conversation",
    "description": (
        "Beendet das Gespraech sofort und still. Rufe es AUSSCHLIESSLICH, wenn "
        "der Mensch dich bittet aufzuhoeren, still zu sein oder Schluss zu "
        "machen — zum Beispiel 'stopp', 'hoer auf', 'sei still', 'danke, das "
        "war's'. Sage dabei NICHTS: kein 'alles klar', keine Verabschiedung, "
        "keine Nachfrage. Der Mensch hat um Ruhe gebeten, und eine Antwort "
        "darauf waere genau das Gegenteil. Rufe es NIE von dir aus."),
    "parameters": {"type": "object", "properties": {}},
}


#: Der Teil der Anweisung, der in JEDEM Modus gilt: Geraete, Ruhe, Abbruch,
#: `weiterweg`, der Arzt, der eine Satz vor langer Arbeit.
_TOOL_INSTRUCTIONS_HEAD = (
    " Du kannst Geraete im Zuhause ueber Werkzeuge steuern: Licht/Schalter an/aus, "
    "Helligkeit setzen, Zustaende lesen, Geraete auflisten. Nutze die Werkzeuge, wenn "
    "der Nutzer eine Steuerung oder Abfrage moechte. Erfinde NIE entity_ids. Ist ein "
    "Geraet mehrdeutig, frage kurz nach. Bestaetige Aktionen knapp auf Deutsch."
    " Bittet dich der Mensch aufzuhoeren, still zu sein oder Schluss zu machen, "
    "dann rufe end_conversation und sage NICHTS dazu. Nicht 'alles klar', nicht "
    "'melde dich, wenn du etwas brauchst' — wer um Ruhe bittet, will keine "
    "Antwort darauf."
    # Live gefunden, und es kostete den Nutzer ein ganzes Gespraech: auf „Stopp,
    # brich den Auftrag ab" beendete das Modell die Unterhaltung, statt den
    # Auftrag abzubrechen. Danach steckte der Verlauf voller „Stopp" und
    # „abbrechen", und jede weitere Frage endete genauso — bis hin zu „wie
    # spaet ist es". Die Stopp-REGEL war unschuldig; sie verlangt, dass die
    # Aeusserung auf einem Stopp-Wort endet. Es war die Anweisung, die
    # `agent_run_cancel` mit keinem Wort erwaehnte.
    " Einen laufenden Auftrag abbrechen ist etwas ANDERES, als das Gespraech zu "
    "beenden. Will der Mensch einen Auftrag nicht mehr — 'brich das ab', 'lass "
    "den Auftrag', 'das brauche ich doch nicht' —, dann nimm agent_run_cancel "
    "und sage kurz, was du abgebrochen hast. end_conversation ist NUR fuer den "
    "Wunsch nach Ruhe. Im Zweifel frage in einem Satz nach, was gemeint ist, "
    "statt still zu werden."
    # Ohne diesen Satz hat das Modell den untersuchten Weg zwar im Ergebnis
    # stehen, sagt aber trotzdem „das kann ich nicht" — es hat gelernt, dass ein
    # Fehlschlag das Ende ist. Der Hinweis sagt ihm, wo die Fortsetzung steht.
    " Schlaegt ein Werkzeug fehl und das Ergebnis enthaelt ein Feld 'weiterweg', dann "
    "sage NICHT bloss, dass du es nicht kannst. Nutze 'weiterweg.antwort' als deine "
    "Antwort: dort steht, was geprueft wurde und was der naechste sinnvolle Schritt "
    "ist. Braucht es eine Freigabe, sage das klar und suche KEINEN anderen Weg daran "
    "vorbei."
    # Der Arzt hat eine eigene Sprachseite. Ohne diesen Satz liest das Modell den
    # Befund vor, wie er ist — mit Zustand, Reparaturklasse und Belegen. Das ist
    # eine Datenstruktur, kein Satz, den jemand hoeren will.
    # Live gefunden am 2026-08-30: „kannst du mal ueberpruefen, warum ich mein
    # Nuki zu Hause nicht nutzen kann?" — das Modell nahm `system_diagnose`,
    # bekam „bei mir ist alles in Ordnung" und riet danach ueber ein fremdes
    # Schloss: Verbindung, Bridge, Batterie. Es hat NICHT nachgesehen, obwohl
    # es die Haus-Werkzeuge hat und Minuten zuvor damit Licht geschaltet hatte.
    # Der Satz hier hiess „Fragt der Nutzer, warum etwas nicht geht" — ohne
    # Rand. Ein Weg ohne Rand zieht an, wofuer er nicht gedacht ist.
    " Geht etwas an DIR selbst nicht — deine Zugaenge, deine Integrationen, "
    "dein eigener Zustand —, nutze system_diagnose und sage nur das Feld "
    "'antwort'. Bittet er dich, etwas an dir zu reparieren, nutze system_heal "
    "und sage ebenfalls nur 'antwort'. Lies NIE Zustaende, Klassen oder Belege "
    "vor."
    " Geht dagegen etwas im Haus oder bei einem fremden Geraet oder Dienst "
    "nicht, ist das NICHT deine Diagnose. Sieh mit der Faehigkeit nach, die es "
    "dafuer gibt, und antworte aus dem, was du gesehen hast. Hast du dafuer "
    "keine Faehigkeit, sage ehrlich, dass du es nicht selbst pruefen kannst — "
    "und rate nicht."
    # Gemessen: eine tiefe Recherche laeuft Minuten. Ohne diesen Satz wartet das
    # Modell auf das Ergebnis und der Mensch steht vor einem stummen Geraet.
    " Dauert etwas erkennbar laenger — Recherche, Fachleute, Portalarbeit —, sage "
    "vorher EINEN kurzen Satz wie 'Ich pruefe das genauer' und halte das Gespraech "
    "nicht an."
    # Diese vier Saetze sind teuer bezahlt. Gemessen am 2026-08-23 und am
    # 2026-08-29: nach 25 Sekunden kam `status: running` zurueck, das Modell
    # erzaehlte es als Ergebnis, und `deep_task_status` wurde in der GESAMTEN
    # Loghistorie null Mal aufgerufen. Ein fertiges Ergebnis wurde nie
    # zugestellt.
    #
    # Der Satz „Ergebnisse langer Arbeit erreichen den Nutzer spaeter ueber die
    # Meldungen" stand hier und war fuer sprachgestartete Recherche FALSCH — es
    # gab keinen solchen Weg. Jetzt gibt es ihn (deep/followup.py), und deshalb
    # darf der Satz wieder dastehen.
    " Ein Rechercheauftrag ist EIN deep_research. Starte nie denselben Auftrag "
    "erneut, solange einer laeuft. Kommt 'abgeschlossen: false' oder "
    "'status: running' zurueck, heisst das LAEUFT, nicht fertig: sag dann, dass "
    "die Recherche noch laeuft und du dich meldest, sobald sie fertig ist. Als "
    "abgeschlossen erzaehlst du NUR ein Ergebnis mit 'abgeschlossen: true'. Das "
    "Ergebnis kommt von selbst — in der laufenden Unterhaltung oder als Meldung; "
    "fragt der Nutzer vorher nach, nutze deep_task_status."
    # „Ich kann nicht recherchieren" bei erschoepftem Kontingent war der
    # gemessene Satz vom 2026-08-29 — zusammen mit ungesicherten Zahlen aus dem
    # Gedaechtnis, ausgegeben als Rechercheergebnis.
    " Kontingent ist kein Koennen: ist das Tagesbudget der Recherche "
    "aufgebraucht, sag genau das — die Faehigkeit gibt es, das Budget erneuert "
    "sich von selbst. Sage NIE 'ich kann nicht im Internet recherchieren', und "
    "gib niemals Zahlen aus dem Gedaechtnis als Rechercheergebnis aus."
)

#: Der Arbeitsabsatz OHNE kognitiven Router: das Modell waehlt selbst unter
#: vier Werkzeugen. Das ist die Fassung von `66b04c5` und die Lage, in die
#: `cognitive_router_mode=off` zurueckfaellt.
_WORK_CHOOSES_MODEL = (
    # Der Nutzer soll SOLVIO ansprechen, nicht dessen Spezialisten. Ohne diesen
    # Absatz standen die Agenten-Werkzeuge zwar bereit, kamen in der Anweisung
    # aber nicht vor — und das Modell waehlte sie nur, wenn jemand sehr deutlich
    # danach fragte. Jede andere Faehigkeit hier hat ihren Satz; diese hatte
    # keinen. Kein Stichwort loest etwas aus, und keine Liste von Formulierungen
    # steht hier: es ist die ART des Ziels, die entscheidet.
    " Manche Ziele brauchen mehr als eine Antwort — mehrere Schritte, Nachsehen "
    "im Code, laengeres Suchen, oder eine Aenderung, die erst vorbereitet werden "
    "muss. So etwas nimmst du als Auftrag an: agent_task_research zum "
    "Herausfinden, agent_task_build zum Bauen oder Reparieren. Tu das von SELBST, "
    "sobald ein Ziel danach klingt. Der Nutzer spricht mit dir, nicht mit deinen "
    "Fachleuten; er muss dich nicht darum bitten und kein bestimmtes Wort sagen. "
    "'Schau mal, warum das nicht geht' oder 'mach mir daraus eine Loesung' "
    "genuegt vollkommen."
    " Umgekehrt gilt genauso: eine einfache Frage beantwortest du selbst, ein "
    "Geraet schaltest du selbst, eine schnelle Einzelfrage klaerst du mit "
    "deep_research, und SOLVIOs eigene Stoerungen bleiben bei system_diagnose. "
    "Ein Auftrag ist fuer das, was laenger dauert als das Gespraech — nicht fuer "
    "alles."
)

#: Derselbe Absatz MIT Router. Die Abgrenzung nach unten steht woertlich wie
#: vorher da — sie ist die Produktanforderung und nicht der Werkzeugname:
#: eine einfache Frage beantwortet das Modell selbst, ein Geraet schaltet es
#: selbst. Was sich aendert, ist nur, dass es die ART der Arbeit nicht mehr
#: waehlen muss.
_WORK_CHOOSES_SOLVIO = (
    " Manche Ziele brauchen mehr als eine Antwort — Nachsehen, Recherche, "
    "mehrere Schritte, oder eine Aenderung, die erst vorbereitet werden muss. "
    "Fuer alles davon nimmst du solvio_task und gibst das Ziel in den Worten "
    "des Nutzers weiter. Du musst NICHT entscheiden, wie es erledigt wird — "
    "das entscheidet SOLVIO selbst. Tu das von SELBST, sobald ein Ziel danach "
    "klingt. Der Nutzer spricht mit dir, nicht mit deinen Fachleuten; er muss "
    "dich nicht darum bitten und kein bestimmtes Wort sagen. 'Schau mal, warum "
    "das nicht geht' oder 'mach mir daraus eine Loesung' genuegt vollkommen."
    " Umgekehrt gilt genauso: eine einfache Frage beantwortest du selbst, ein "
    "Geraet schaltest du selbst, und SOLVIOs eigene Stoerungen kannst du "
    "weiterhin direkt mit system_diagnose ansehen. Ein Auftrag ist fuer das, "
    "was laenger dauert als das Gespraech — nicht fuer alles."
    # **Womit du selbst antwortest, war bisher nicht gesagt.** Gemessen am
    # 07.09.2026: auf „Wie hoch steht der DAX heute?" griff das Modell sechsmal
    # zum Browser und lieferte in 20 Sekunden keine Zahl. „Beantworte das
    # selbst" ohne benannten Weg heisst: such dir einen.
    " Aktuelle Sachfragen — Kurs, Wetter, Ergebnis, Termin, Zahl, Stand einer "
    "Sache — beantwortest du mit research_quick. Das ist der Rechercheweg, "
    "und er bringt Quelle und Zeitstand mit; nenne beide. Die "
    "Browserwerkzeuge sind fuer eine BESTIMMTE Seite, deren Adresse feststeht "
    "— nicht zum Suchen. Gib research_quick immer eine vollstaendige Frage: "
    "loese Bezuege aus dem Gespraech selbst auf, mit Thema und konkretem "
    "Datum."
    " Kommt aus solvio_task ein Feld 'hinweis' zurueck, dann befolge es: es "
    "sagt dir, ob du selbst antwortest, genau eine Rueckfrage stellst oder "
    "ein Ergebnis wiedergibst."
)

#: Was er nicht hoeren will.
_TOOL_INSTRUCTIONS_TAIL = (
    # Was er nicht hoeren will.
    " Erzaehle nicht, welches Werkzeug du benutzt, und denke nicht laut. Keine "
    "Fuellsaetze wie 'einen Moment' vor jeder Antwort. Wartest du auf eine Freigabe, "
    "sage es EINMAL und frage nicht nach.")

#: Die Anweisung ohne Router — unveraendert die Zeichenkette von vorher.
TOOL_INSTRUCTIONS = (_TOOL_INSTRUCTIONS_HEAD + _WORK_CHOOSES_MODEL
                     + _TOOL_INSTRUCTIONS_TAIL)

#: Die Anweisung mit Router.
COGNITION_TOOL_INSTRUCTIONS = (_TOOL_INSTRUCTIONS_HEAD + _WORK_CHOOSES_SOLVIO
                               + _TOOL_INSTRUCTIONS_TAIL)


def tool_instructions_for(mode: str) -> str:
    """Welche Anweisung diese Sitzung bekommt.

    Sie wird bei `_configure` gelesen und gilt fuer die Lebenszeit einer
    Anbietersitzung — es gibt in diesem Core kein zweites `session.update`,
    und dieser Milestone fuehrt auch keines ein. Ein Moduswechsel wirkt
    deshalb ab der naechsten Sitzung, was zur Rollback-Zusage passt: der
    Schalter steht in der Konfiguration und wirkt beim Start.

    `shadow` bekommt ausdruecklich die ALTE Anweisung: der Schattenmodus misst
    und aendert nichts, auch nicht das, was das Modell liest.
    """
    return (COGNITION_TOOL_INSTRUCTIONS if str(mode or "") == "active"
            else TOOL_INSTRUCTIONS)

# Silent-Stop-Kommandos: sofort still + Session beenden, KEINE Assistentenantwort.
# M0/§A: an utterance ends the session only if the WHOLE utterance is a stop expression.
#
# The previous rule was `len(words) <= 3 and any(word in _STOP_WORDS)`, and `_STOP_WORDS`
# contained "aus". In German "aus" is a separable verb particle — "das Licht *aus*machen",
# "den Fernseher *aus*schalten" — so every short device command ending in it closed the
# session silently: "licht aus", "alles aus", "musik aus", "fernseher aus", "alarm aus".
# It was also inconsistent along the wrong axis: "mach das licht aus" (four words) survived
# while "licht aus" (two) did not, for no reason a user could predict.
#
# And it was wrong in the other direction too: containment never matched real stop phrases
# like "hör auf", "abbrechen" or "das war's", so those fell through to normal processing.
#
# Whole-utterance matching removes both failures without a phrase dictionary: a command
# always carries an object ("licht", "musik", "der fernseher"), so it can never BE a stop
# expression. No model call, no heuristics on length.
_STOP_UTTERANCES = frozenset({
    "stop", "stopp", "stoppe", "stoppen",
    "hör auf", "hoer auf", "höre auf", "hoere auf", "hör bitte auf", "hoer bitte auf",
    "abbrechen", "brich ab", "abbruch",
    "das wars", "das war alles", "das wars dann",
    "danke das wars", "danke das war alles", "danke das wars dann",
    "ruhe", "sei ruhig", "sei leise", "sei still", "leise", "still",
    "schluss", "schluss jetzt",
})
# Ambiguous on their own — "aus" is the particle above, "halt" is usually a filler in German.
# They count as a stop ONLY when the user addressed SOLVIO directly, which is what the
# previous `solvio aus` / `solvio halt` entries expressed.
_ADDRESSED_ONLY_STOPS = frozenset({"aus", "halt"})
# Fuellwoerter, die einen Stopp nicht zu etwas anderem machen.
#
# Gemeldet: „er reagiert nicht auf mein Stopp, er sagt ja, ich stoppe — und
# quatscht weiter." Die Erkennung verlangte, dass die Aeusserung EXAKT ein
# Stopp-Wort ist. „Solvio, stopp mal", „bitte stopp", „stopp jetzt" und „sei
# mal still" fielen alle durch und gingen als gewoehnliche Frage ans Modell,
# das brav darauf antwortete.
#
# Sie werden vor dem Vergleich entfernt, der Vergleich selbst bleibt EXAKT.
# Ein ganzer Satz, der ein Stopp-Wort nur enthaelt („ich wollte nicht, dass du
# aufhoerst"), loest deshalb weiterhin nichts aus.
_STOP_FILLERS = frozenset({
    "mal", "bitte", "jetzt", "kurz", "doch", "einfach", "ok", "okay",
    "so", "schon", "denn", "eben",
})
# Die Anrede in ihrer KORREKTEN Schreibweise. Mehr nicht.
#
# Es gab hier einmal eine Liste von Verhoerern — „solve your", „zovi", „olwe".
# Sie war der falsche Weg: der Name kam bei fuenf Versuchen fuenfmal anders an,
# und eine Liste, die dem hinterherlaeuft, ist nie fertig. Die Kontrollspur
# unten kommt ohne den Namen aus.
_ADDRESS_PREFIXES = tuple(sorted(
    {f"{g} solvio" for g in ("hallo", "okay", "hey", "ok", "he")} | {"solvio"},
    key=len, reverse=True))


_ELISION = str.maketrans("", "", "'\u2019\u02bc\u00b4`")


# M0/§C: `session_open_failed` logged only `kind=RuntimeError`, so an operator could not
# tell an expired API key from a network timeout from a rejected session config — even
# though `Session.open` already puts the provider's own error text into the exception it
# raises. The detail was thrown away at the log call, not at the raise.
#
# The Authorization header goes into `ws_connect`, so a provider exception can quote it back.
# Redaction happens BEFORE anything reaches the log.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*\S+"),
    # Anything that LOOKS like a credential rather than like prose. Deliberately last.
    #
    # A blunt "20+ key characters" rule was tried first and live validation caught it eating
    # `billing_hard_limit_reached` — a provider error code, i.e. exactly the diagnostic an
    # operator needs. Long snake_case identifiers are prose; credentials are not. So:
    #   * 20+ characters that mix a digit AND an upper-case letter (sk-proj-AbC0123...), or
    #   * 32+ characters of any shape (hex and base64 tokens are long and lower-case).
    re.compile(r"\b(?=[A-Za-z0-9_\-]{20,}\b)(?=[A-Za-z0-9_\-]*[0-9])"
               r"(?=[A-Za-z0-9_\-]*[A-Z])[A-Za-z0-9_\-]+\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
)
_MAX_DIAGNOSTIC = 300


def sanitize_diagnostic(text: str, *, limit: int = _MAX_DIAGNOSTIC) -> str:
    """Strip credential-shaped values from provider text so it is safe to log."""
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("<redacted>", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


_MAX_AUDIO_DROP_LOGS = 5


def describe_exception(exc: BaseException) -> dict:
    """Type plus a sanitized message — what an operator needs, without the secrets.

    A cause is included when the provider wrapped one, because that is usually where the
    real reason lives (an HTTP status, a refused connection, a timeout).
    """
    info = {"kind": type(exc).__name__, "detail": sanitize_diagnostic(str(exc))}
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        info["cause_kind"] = type(cause).__name__
        info["cause_detail"] = sanitize_diagnostic(str(cause), limit=160)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        info["status"] = status
    return info


def _normalise_utterance(transcript: str) -> str:
    """Lowercase, drop punctuation and digits, collapse whitespace. Umlauts survive."""
    # `casefold` faltet ss aus ss-Ligatur mit — ohne das waren Eintraege wie
    # „heisst" tot, weil eine deutsche Spracherkennung „heisst" mit
    # ss-Ligatur schreibt und die Zeichenklasse sie erhielt.
    text = (transcript or "").casefold().translate(_ELISION)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\u00e4\u00f6\u00fc ]+", " ",
                                       text)).strip()


def _strip_address(text: str) -> tuple[str, bool]:
    """Split off an address to SOLVIO — vorne ODER hinten. Returns (rest, was_addressed).

    Hinten war lange nicht vorgesehen, und gemessen wurde genau das:
    `stop_near_miss addressed=False` bei Aeusserungen, die SOLVIO sehr wohl
    ansprachen — nur eben als „Stopp, Solvio" statt „Solvio, stopp". Beides ist
    dieselbe Bitte, und wer sie stellt, sortiert nicht nach Satzbau.
    """
    for prefix in _ADDRESS_PREFIXES:
        if text == prefix:
            return "", True
        if text.startswith(prefix + " "):
            return text[len(prefix) + 1:].strip(), True
        if text.endswith(" " + prefix):
            return text[: -(len(prefix) + 1)].strip(), True
    return text, False


def _without_fillers(body: str) -> str:
    """Entfernt Hoeflichkeits- und Fuellwoerter. Der Rest muss weiterhin exakt passen."""
    words = [w for w in body.split() if w not in _STOP_FILLERS]
    return " ".join(words)


#: Woerter, die VOR einem „stopp" aus ihm etwas anderes machen.
#:
#: Ohne sie wuerde „was bedeutet stopp" das Gespraech beenden. Die Liste ist
#: bewusst klein und deckt genau drei Faelle: eine FRAGE nach dem Wort, eine
#: WIEDERGABE fremder Rede, und eine BEDINGUNG.
_NOT_A_COMMAND_BEFORE_STOP = frozenset({
    # Frage nach dem Wort
    "was", "wie", "warum", "wieso", "weshalb", "wozu", "wer", "wen", "wem",
    "wann", "wo", "welche", "welcher", "welches", "bedeutet", "bedeuten",
    "heisst", "heissen", "meint", "meinst", "meinte",
    # Wiedergabe fremder Rede
    "sagt", "sagte", "sagst", "sage", "sagen", "gesagt", "nennt", "schreibt",
    "buchstabiert", "steht", "stand",
    # Bedingung und Verneinung
    "wenn", "falls", "ob", "weil", "dass", "obwohl", "nicht", "kein", "keine",
    # Befehlsform mit Gegenstand. „mach stop" hat die Gestalt eines
    # Geraetebefehls („mach licht an"), und eine bestehende Zusicherung haelt
    # ausdruecklich fest, dass es das Gespraech nicht beendet.
    "mach", "machen", "schalte", "stell", "stelle", "setz", "setze",
    # Redewiedergabe im BEFEHL. Mit einem Assistenten spricht man im
    # Imperativ — „sag stopp" heisst, er soll das Wort sagen, nicht aufhoeren.
    # Fuer Geraetebefehle standen die Imperativformen von Anfang an in der
    # Liste, fuer Redeverben nicht.
    "sag", "schreib", "schreibe", "nenn", "nenne", "antworte", "wiederhol",
    "wiederhole", "diktier", "diktiere", "buchstabier", "buchstabiere",
    # Aussage ueber etwas, nicht Anweisung. „das war stopp" beendet nichts.
    "das", "war", "ist", "wars",
})

#: DER GEGENSTAND, der gestoppt werden soll — vorangestellt und ohne Verb.
#:
#: Das ist die Form, die deutsche Kurzbefehle wirklich haben: „Musik stopp",
#: „Timer stopp", „Rollladen stopp". Die Sperrliste war entlang der WORTART
#: gebaut (Fragewoerter, Redeverben, Befehlsformen) und hat diese Gestalt nie
#: gesehen — obwohl der aeltere Kommentar zu den Stopp-Woertern die richtige
#: Regel noch nennt: ein Befehl traegt IMMER einen Gegenstand und kann deshalb
#: nie selbst ein Stopp sein.
#:
#: „Alexa" und „Siri" stehen mit drin: „Alexa, stopp!" gilt einem anderen
#: Geraet im selben Raum und geht SOLVIO nichts an.
_STOPPABLE_OBJECT_BEFORE_STOP = frozenset({
    "musik", "lied", "song", "radio", "fernseher", "tv", "video", "podcast",
    "playlist", "hoerbuch", "licht", "lampe", "timer", "wecker", "alarm",
    "countdown", "stoppuhr", "aufnahme", "rollladen", "rolladen", "jalousie",
    "rollo", "tor", "garagentor", "staubsauger", "sauger", "pumpe",
    "ventilator", "kaffeemaschine", "heizung", "download", "browser",
    "recherche", "alexa", "siri", "sonos", "spotify",
})

#: Wie lang eine Abbruchanweisung hoechstens ist, NACH dem Entfernen von
#: Fuellwoertern. Alles darueber ist ein Satz und keine Anweisung. Drei, weil
#: die gemessenen Transkripte drei Zeichen brauchten: die verhoerte Anrede
#: belegte bis zu zwei davon („fang ich stopp").
_STOP_MAX_WORDS = 3

#: Wie lange nach dem ersten Ton einer Antwort das Mikrofon NICHT an den
#: Anbieter geht.
#:
#: Gemeldet: „faengt an, sagt einen Satz, bricht ab, faengt von vorn an." Acht
#: abgebrochene Antworten in einer Sitzung, davon KEINE vom Endgeraet — es war
#: die Spracherkennung des Anbieters, die den auslaufenden Satz des Menschen
#: hoerte, waehrend die Antwort schon lief, und ihn fuer eine neue Runde hielt.
#:
#: Es ist dasselbe Mass, das auf dem Telefon fuer das lokale Unterbrechen gilt:
#: niemandem ins Wort fallen, bevor er etwas gesagt hat. Es steht HIER und
#: nicht dort, weil es sonst nur fuer einen der beiden Endpunkte gaelte — der
#: Satellit hatte es nicht, und genau dort trat der Fehler zuletzt auf.
UTTERANCE_GUARD_SECONDS = 0.7

#: Die einzigen Woerter, die diese Spur ausloesen. Bewusst zwei.
_HARD_STOP_WORDS = frozenset({"stop", "stopp"})


def looks_like_a_device_command(transcript: str) -> bool:
    """Ob die Aeusserung einen GEGENSTAND in der Welt meint, nicht das Gespraech.

    Gemessen: „Fernseher aus" beendete zweimal das Gespraech. Nicht ueber die
    Kontrollspur — die laesst es korrekt durch —, sondern weil das MODELL es
    als Abschied deutete und `end_conversation` rief. Der zweite Weg hatte
    keine Bremse.

    Das hier ist die Bremse, und sie gehoert dem Core: das Modell darf den
    Abbruch anfragen, aber nicht, wenn der Mensch gerade ueber ein Geraet
    gesprochen hat. Ein Werkzeugaufruf ist eine Bitte, keine Entscheidung.
    """
    words = _normalise_utterance(transcript).split()
    if not words:
        return False
    if any(w in _STOPPABLE_OBJECT_BEFORE_STOP for w in words):
        return True
    # Der nachgestellte Partikel ist die haeufigste deutsche Befehlsform:
    # „Licht aus", „Fernseher an", „alles aus".
    return len(words) <= 4 and words[-1] in ("aus", "an", "ein", "zu", "hoch",
                                             "runter", "heller", "dunkler")


def is_conversation_stop(transcript: str) -> bool:
    """Die Kontrollspur des Cores: eine unmissverstaendliche Abbruchanweisung.

    Sie kommt OHNE den Namen aus, und das ist ihr ganzer Zweck. Gemessen kam
    derselbe gesprochene Name bei fuenf Versuchen fuenfmal anders an — „solve
    your", „zovi", „olwe", „fang ich", „halt er". Eine Erkennung, die auf ihn
    wartet, wartet vergebens; eine Liste, die ihm hinterherlaeuft, ist nie
    fertig.

    Verlaesslich ist dagegen das STOPP-Wort selbst. Die Regel lautet deshalb:

    * hoechstens drei Woerter, nachdem Fuellwoerter entfernt sind,
    * das LETZTE davon ist genau „stop" oder „stopp",
    * und davor steht nichts, was daraus eine Frage, eine Wiedergabe oder eine
      Bedingung macht.

    Was davor steht, muss NICHT verstanden werden — es darf Rauschen sein. Das
    ist der Unterschied zu jedem frueheren Anlauf.

    Ausdruecklich KEIN Abbruch sind Geraetebefehle: „alles aus", „fernseher
    aus", „licht aus", „stoppe den fernseher". Sie enden nicht auf einem
    Stopp-Wort oder sind zu lang, und beides ist Absicht.
    """
    text = _normalise_utterance(transcript)
    if not text:
        return False
    words = _without_fillers(text).split()
    if not words:
        return False
    # Reine Wiederholung: „Stopp! Stopp! Stopp!" — je draengender, desto
    # laenger, und die Laengengrenze machte die Regel ausgerechnet dann
    # strenger. Eine Aeusserung, die NUR aus Stopp-Woertern besteht, kann kein
    # Geraetebefehl, keine Frage und keine Wiedergabe sein: dort steht immer
    # ein Gegenstand oder ein Fragewort dabei.
    if all(w in _HARD_STOP_WORDS for w in words):
        return True
    if len(words) > _STOP_MAX_WORDS:
        return False
    if words[-1] not in _HARD_STOP_WORDS:
        return False
    head = words[:-1]
    if any(w in _NOT_A_COMMAND_BEFORE_STOP for w in head):
        return False
    return not any(w in _STOPPABLE_OBJECT_BEFORE_STOP for w in head)


def is_silent_stop(transcript: str) -> bool:
    """True only for an utterance that IS a request to end the interaction."""
    text = _normalise_utterance(transcript)
    if not text:
        return False
    body, addressed = _strip_address(text)
    if not body:
        return False            # the wake word alone is not a stop
    for candidate in (body, _without_fillers(body)):
        if not candidate:
            continue
        if candidate in _STOP_UTTERANCES:
            return True
        if addressed and candidate in _ADDRESSED_ONLY_STOPS:
            return True
    # Knapp daneben: ein kurzer Satz, der ein Stopp-Wort enthaelt, aber nicht
    # als einer erkannt wurde. Nur die WORTZAHL und das erkannte Wort gehen ins
    # Log, nie der Satz — es soll sichtbar sein, dass etwas dicht daneben lag,
    # ohne dass Gespraechstext im Betriebslog landet.
    stripped = _without_fillers(body)
    words = stripped.split()

    # Eine kurze Aeusserung, deren einziges Wort mit „stop" anfaengt, IST ein
    # Stopp — egal in welcher Beugung die Erkennung sie geschrieben hat
    # („stoppe", „stoppt", „stoppen"). Genau eine Wortform zu verlangen hiess,
    # sich auf die Laune eines Transkribierers zu verlassen.
    if len(words) == 1 and words[0].startswith("stop"):
        return True

    # Und jetzt die Auskunft: ALLES, was kurz ist und auch nur entfernt nach
    # Stopp oder nach dem Namen klingt, ohne gegriffen zu haben. Drei Runden
    # Nachbessern gingen daneben, weil die Auskunft zu eng gefasst war und
    # ausgerechnet den Fall nicht zeigte, der eintrat.
    # WORTgleichheit, nicht Teilstring. „auf" und „halt" stecken in
    # gewoehnlicher Rede („was laeuft auf netflix"), und die Teilstring-Suche
    # zog damit einen spuerbaren Anteil normaler Aeusserungen in das Log —
    # WOERTLICH, obwohl der Kommentar hier „nie der Satz" verspricht. Dieselbe
    # Datei begruendet bei `assistant_utterance` ausdruecklich, warum
    # Gespraechstext nicht ins Betriebslog gehoert.
    hit = next((k for k in ("stop", "stopp", "still", "halt", "ruhe", "auf",
                            "leise", "schluss") if k in words), "")
    if hit and len(words) <= 5:
        log.info("core.stop_near_miss", words=len(words),
                 addressed=addressed, matched=hit)
    return False


def _settled(result: dict) -> bool:
    """Ist mit diesem Werkzeugergebnis die Sache erledigt?

    Vorgabe ist JA — die allermeisten Werkzeuge sind mit ihrer Rueckgabe
    fertig. Nein sagt nur, wer es ausdruecklich sagt: `abgeschlossen: false`
    im Datenfeld. Heute setzt das genau die tiefe Recherche nach ihren ersten
    25 Sekunden.
    """
    data = result.get("data")
    return not (isinstance(data, dict) and data.get("abgeschlossen") is False
                and data.get("status") == "running")


def _deep_task_running(result: dict) -> str:
    """Die Kennung einer Recherche, die WEITERLAEUFT — sonst leer.

    Genau diese Lage braucht einen Beobachter: das Werkzeug ist zurueck, die
    Arbeit nicht fertig, und ohne Zustellweg endet das Ergebnis im Nichts.
    """
    if not result.get("success"):
        return ""
    data = result.get("data")
    if not isinstance(data, dict) or data.get("abgeschlossen") is not False:
        return ""
    if data.get("status") != "running":
        return ""
    return str(data.get("task_id", "") or "")


def _deep_task_cancelled(result: dict) -> str:
    """Die Kennung einer abgebrochenen Recherche — sonst leer."""
    data = result.get("data")
    if not result.get("success") or not isinstance(data, dict):
        return ""
    if data.get("status") != "cancelled":
        return ""
    return str(data.get("task_id", "") or "")


class Session:
    """Eine aktive Gespraechssitzung fuer genau eine Pi-Verbindung."""

    def __init__(self, server: "CoreServer", ws: Any) -> None:
        self.server = server
        self.ws = ws
        self.oa: Any = None
        self.up = Resampler(SATELLITE_RATE, OPENAI_RATE)
        self.down = Resampler(OPENAI_RATE, SATELLITE_RATE)
        self.active = False
        self.speaking = False
        self.responding = False
        self.last_activity = time.monotonic()
        self.reader: Any = None
        self.timer: Any = None
        self._closing = False
        self._closing_task: asyncio.Task | None = None
        self._stopping = False
        self.open_stage = "idle"
        self.open_attempts = 0
        # M0/3: die kleinste Korrelationsidentitaet, mit der sich EINE Sitzung und EIN Turn
        # durch die Logs verfolgen laesst. Kein Tracing-Framework, nur zwei Bezeichner.
        self.session_id = "s-" + secrets.token_hex(4)
        self.turn_no = 0
        # Aus der Authentifizierung, nie aus einem Modellargument. Leer heisst:
        # kein bewiesener Aufrufer, also nichts Wirksames.
        self.satellite_id = ""
        self.audio_device_id = ""
        #: Woher dieser Turn kommt — und was das ueber den SPRECHER aussagt.
        #:
        #: Die Vorgabe ist der Satellit, also ein RAUMMIKROFON: der Endpunkt ist
        #: authentifiziert, der Sprecher nicht. Aus dieser Klasse lernt Adaptive
        #: Memory NICHTS automatisch (`policy.SourceClass`). Der Sprachendpunkt
        #: des iPhones setzt sie nach bewiesener Geraetepruefung um.
        #:
        #: Fail-closed durch die Vorgabe: wer einen neuen Endpunkt anhaengt und
        #: das Setzen vergisst, bekommt die vorsichtigere Klasse, nicht die
        #: groesszuegigere.
        self.channel = "voice_satellite"
        #: Hat diese Sitzung einen App-Attest-Sitzungsbeweis vorgelegt? Nur
        #: `/v1/voice` kann das setzen, und nur nach erfolgreicher Pruefung.
        #: Fail-closed: wer es nicht setzt, bekommt die strengere Zeile.
        self.interactive_proof = False
        self.app_task_session = None
        self.browser_task_session = None
        # Was der Nutzer in DIESEM Turn wirklich gesagt hat. Grundlage dafuer, ob ein
        # Werkzeugargument vom Nutzer stammt oder das Modell es sich ausgedacht hat.
        self.turn_user_text = ""
        #: Steht der Nutzertext dieses Turns fest? Siehe `_await_turn_text`.
        self.turn_text_ready = asyncio.Event()
        # Alle Dauern kommen aus time.monotonic(): eine Zeitumstellung oder ein NTP-Sprung
        # darf eine Latenzmessung nicht verfaelschen. Wanduhr bleibt nur im Log-Zeitstempel.
        self.t_connected = time.monotonic()
        self.t_auth = None
        self.t_session_start = None
        self.t_provider_open = None
        self.t_provider_ready = None
        self._turn = {}
        # M0/3: Tool-Ausfuehrung gehoert NICHT in den Provider-Reader. Eine Queue plus genau
        # ein Worker: der Reader bleibt lesebereit, die Reihenfolge bleibt erhalten, und es
        # gibt nur eine Task, die beim Schliessen abgeraeumt werden muss.
        #: Turns, die der Schatten schon gesehen hat. Ein Turn, eine Zeile.
        self._shadow_seen: set[str] = set()
        self._tool_queue: asyncio.Queue = asyncio.Queue()
        self._tool_worker = None
        # Tasks der vorigen Generation, die noch auslaufen. open() wartet sie ab,
        # bevor eine neue Generation beginnt.
        self._draining: list = []
        # M0/4: verworfene Provider-Audio-Frames dieser Sitzung.
        self.dropped_audio_frames = 0
        # Der Satzanfang. Zwischen „der Satellit ist da" und „der Anbieter kann
        # hoeren" lagen gemessen 530 bis 1675 ms, und in dieser Zeit warf
        # `feed_audio` jedes Frame weg — elf von 42 Sitzungen endeten mit
        # turns=0. Was hier hineinlaeuft, geht beim Bereitwerden in Reihenfolge
        # an den Anbieter. Fluechtig, begrenzt, und beim Schliessen geloescht.
        self.preroll = Preroll(rate=SATELLITE_RATE)
        # Die laufende Oeffnung. Sie ist eine Task, keine awaitete Zeile — siehe
        # `_handle_pi`. Ohne diese Referenz koennte sie beim Schliessen
        # weiterlaufen und in eine tote Sitzung hineinschreiben.
        self._open_task = None
        # Ob dem Satelliten schon gesagt wurde, dass Schluss ist. Er wartet
        # nach einem `session_start` auf eine Antwort; bekommt er keine, bleibt
        # er in ACTIVE stehen.
        self._told_satellite = False
        self._reconnect_task = None
        # Eingangsmessung: wie viele Frames kamen an, bevor der Anbieter
        # bereit war, und wann das erste. Zahlen, nie Inhalt.
        self._frames_in = 0
        #: Wie viele Rahmen die Schonfrist am Antwortanfang zurueckgehalten hat.
        self._guarded_frames = 0
        # Ueberschreibt `server.eagerness`, wenn ein Endpunkt es setzt.
        # `None` heisst: nimm den Wert des Servers.
        self.eagerness: str | None = None
        #: Ob beim Sitzungsstart erwaehnt wird, dass Hintergrundmeldungen da
        #: sind. Ein Endpunkt mit eigenem Bildschirm schaltet das ab: dort
        #: stehen sie ohnehin, mit Zaehler, und muessen nicht vorgelesen
        #: werden. Ein Satellit ohne Bildschirm behaelt es — dort waere
        #: Weglassen kein Aufraeumen, sondern Informationsverlust ohne Ersatz.
        self.mention_proactive = True
        self._frames_before_ready = 0
        self.t_first_frame = None
        # Wem das Audio gehoert, das gerade hereinkommt.
        #
        # `semantic_vad` mit `interrupt_response` bricht die Antwort beim
        # Anbieter ab — aber was schon unterwegs ist, kommt trotzdem an, und
        # frueher wurde es weitergereicht. Der Satellit bekam nach dem `flush`
        # noch Reste der abgebrochenen Antwort und spielte sie ab: die alte
        # Stimme redete ueber die neue Frage.
        #
        # Zwei Sperren statt einer, weil sie verschiedene Faelle fangen: das Tor
        # schliesst sofort und ohne Kenntnis der Kennung, die Totenliste faengt
        # ein Delta, das nach dem Beginn der NAECHSTEN Antwort noch eintrudelt.
        self._response_id = ""
        #: Welchen Gegenstand der Anbieter gerade spricht. Gebraucht wird er
        #: genau einmal: um beim Unterbrechen zu sagen, wie viel der Mensch
        #: davon WIRKLICH gehoert hat.
        self._audio_item = ""
        self._audio_index = 0
        self._dead_responses: deque[str] = deque(maxlen=8)
        self._audio_open = True
        # M1: Ein SOLVIO-Gespraech ueberlebt die Provider-Sitzung. conversation_id ist
        # NICHT session_id — die eine gehoert dem Produkt, die andere dem Transport.
        # conversation_mode: "active" (Store traegt), "degraded" (Store hat versagt, die
        # Sitzung laeuft zustandslos weiter) oder "off" (kein Store konfiguriert).
        self.conversation_id: str | None = None
        self.conversation_mode = "off"
        self.context_messages = 0
        self.context_chars = 0
        #: N8/C3: der explizite Chat, den der Endpunkt fuer diese Sitzung gebunden
        #: hat (Browser-Ticket bzw. iPhone-Assertion). Leer heisst Linger wie
        #: bisher. Gesetzt wird er NUR vom Endpunkt nach bestandener
        #: Eigentumspruefung, nie aus einer Nachricht des Anbieters.
        self.bound_conversation_id = ""
        self.authenticated_room_device = ""
        #: Was die App in ihrer Assertion verlangt hat — geprueft wird es im
        #: Endpunkt, bevor die Nachrichtenschleife beginnt.
        self.requested_conversation_id = ""
        # Gemessen: ein normaler Schreibvorgang kostet 0,1 ms — im Alltag nichts. Der
        # Schwanz ist aber nur durch busy_timeout begrenzt (5 s), und M0/3 hat gezeigt, was
        # ein blockierter Reader anrichtet: Audio, speech_started/stopped, Transkripte und
        # der Silent-Stop kommen alle ueber denselben Socket. Mit einem absichtlich
        # langsamen Store (250 ms) erreichte das folgende Audio-Delta den Satelliten erst
        # nach 252 ms. Deshalb: Warteschlange, EIN Worker, SQLite in einem Thread.
        self._persist_queue: asyncio.Queue = asyncio.Queue()
        self._persist_worker = None
        self._persist_pending = 0
        self.persist_failures = 0
        self._handoff_prior_unconfirmed = False

    def observe_authenticated_device(self, device_id: str) -> None:
        """Only the authenticated endpoint supplies this full device identity."""
        self.audio_device_id = device_id
        observer = getattr(self.server, "audio_observations", None)
        if observer is not None:
            observer.connect(device_id, self.session_id, self.channel)

    def _observe_audio(self, event: str, generation: int | None = None) -> None:
        observer = getattr(self.server, "audio_observations", None)
        if observer is not None and self.audio_device_id:
            observer.event(self.audio_device_id, self.session_id,
                           self.open_attempts if generation is None else generation, event)

    def audio_disconnected(self) -> None:
        observer = getattr(self.server, "audio_observations", None)
        if observer is not None and self.audio_device_id:
            observer.disconnect(self.audio_device_id, self.session_id)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def open(self, *, announce: bool = True) -> None:
        """Die Anbietersitzung aufbauen.

        `announce=False` beim Wiederaufbau nach einem Abriss: der Satellit
        steht dann bereits in ACTIVE und sendet. Ihm ein zweites
        `session_ready` zu schicken hiesse, ihm eine Zustandsaenderung zu
        melden, die es nicht gab — und wie er darauf reagiert, ist von hier aus
        nicht pruefbar. Der Wiederaufbau bleibt fuer ihn unsichtbar; er merkt
        nur, dass es kurz still war.
        """
        # Zwei verschiedene Faelle, zwei verschiedene Antworten.
        #
        # Laeuft gerade ein close()? Dann ist die alte Generation noch nicht abgeraeumt.
        # Konservativ ablehnen statt zwei Generationen nebeneinander laufen zu lassen; der
        # Satellit baut ohnehin pro Aufwachen eine neue Verbindung auf.
        if self._closing:
            log.warning("core.session_start_ignored", session_id=self.session_id,
                        reason="closing", attempt=self.open_attempts + 1)
            return
        # Ist bereits eine Sitzung offen? Dann ist ein zweites session_start ein Duplikat.
        # NICHT die laufende Sitzung dafuer abreissen — ein Duplikat ist kein Auftrag zum
        # Neustart. Ignorieren und weiterlaufen.
        if self.reader is not None:
            log.warning("core.session_start_ignored", session_id=self.session_id,
                        reason="already_open", attempt=self.open_attempts + 1)
            return
        # Die vorige Generation wirklich einsammeln, bevor eine neue beginnt. close() stoesst
        # das Canceln nur an; ohne dieses Abwarten koennte ein auslaufender Reader noch in
        # die neue Sitzung hineinschreiben.
        await self._drain()
        # Frische Generation: alles, was pro Sitzung gilt, faengt neu an. Insbesondere
        # _stopping — eine per Silent-Stop beendete Sitzung haette die naechste sonst
        # dauerhaft stummgeschaltet, weil der Audio-Pfad daran haengt.
        self._stopping = False
        self.speaking = False
        self.responding = False
        self._turn = {}
        t0 = time.monotonic()
        self.open_attempts += 1
        audio_generation = self.open_attempts
        self._observe_audio("opening", audio_generation)
        self.t_session_start = t0
        log.info("core.session_start", session_id=self.session_id, attempt=self.open_attempts)
        url = self._provider_url()
        # M0/§C: which STEP failed is half the diagnosis — a refused connect, a rejected
        # handshake and a bad session config look identical in the log without it.
        self.open_stage = "connect"
        self.t_provider_open = time.monotonic()
        guard = getattr(self, "acceptance_guard", None)
        if guard is None:
            self.oa = await ws_connect(
                url, additional_headers={"Authorization": f"Bearer {self.server.api_key}"},
                open_timeout=20, max_size=None)
        else:
            self.oa = await guard.connect(ws_connect, url,
                additional_headers={"Authorization": f"Bearer {self.server.api_key}"},
                open_timeout=20, max_size=None)
        await self._prepare_provider()
        self.open_stage = "ready"
        self.t_provider_ready = time.monotonic()
        self.reader = asyncio.create_task(self._oa_reader(audio_generation=audio_generation))
        # Timer and persistence belong to the conversation, not to one provider
        # socket. Reconnect must retain their references rather than orphan them.
        if self.timer is None or self.timer.done():
            self.timer = asyncio.create_task(self._timeout_loop())
        self._tool_worker = asyncio.create_task(self._tool_loop())
        if self._persist_worker is None or self._persist_worker.done():
            self._persist_worker = asyncio.create_task(self._persist_loop())
        self.active = True
        self._observe_audio("ready", audio_generation)
        self.touch()
        # Erst der gehaltene Satzanfang, dann alles Weitere. Die Reihenfolge ist
        # die halbe Zusage dieses Puffers.
        await self._flush_preroll()
        if not self.active:
            return  # The reader may have ended while the buffered audio was sent.
        if announce:
            await self.ws.send(json.dumps(self._ready_message()))
        # M0/3: das Setup aufgeschluesselt, damit "warum 1,3 s?" beantwortbar wird.
        log.info("core.session_ready", session_id=self.session_id,
                 conversation_id=self.conversation_id,
                 conversation_mode=self.conversation_mode,
                 setup_ms=self._ms(t0, time.monotonic()),
                 auth_to_start_ms=self._ms(self.t_auth or self.t_connected, t0),
                 prepare_ms=self._ms(t0, self.t_provider_open),
                 provider_connect_and_handshake_ms=self._ms(self.t_provider_open,
                                                            self.t_provider_ready),
                 configure_and_ready_ms=self._ms(self.t_provider_ready, time.monotonic()),
                 # Die Frage, an der dieser ganze Umbau haengt: hat der Satellit
                 # waehrend des Oeffnens ueberhaupt geschickt? Frueher konnte
                 # die Schleife das nicht einmal sehen, weil sie stillstand.
                 frames_before_ready=self._frames_before_ready,
                 first_frame_after_start_ms=self._ms(self.t_session_start,
                                                     self.t_first_frame),
                 **self.preroll.stats())

    def _ready_message(self) -> dict:
        message = {"type": "session_ready", "voice_mode": "full_duplex" if self.server.model == "gpt-live-1" else "turn_based"}
        if (self._conversation_handoff_supported()
                and self.channel in {"voice_iphone", "voice_browser"} and self.conversation_id):
            message.update(session_id=self.session_id, conversation_id=self.conversation_id,
                           handoff_protocol=1)
        return message

    def _conversation_handoff_supported(self) -> bool:
        # A normal close of the legacy Realtime socket does not confirm that
        # the provider delivered the last input transcript.
        return False

    async def _confirm_conversation_flush(self, *, provider_closed: bool) -> None:
        """Confirm the voice/text boundary after the provider's final transcript.

        session_end stops local audio immediately; it precedes provider shutdown
        and cannot prove that text has reached the canonical conversation. The
        bound chat and session identify this later acknowledgement. It conveys
        no task authority and never upgrades a failed/partial write to success.
        """
        if self.channel not in {"voice_iphone", "voice_browser"} or not self.conversation_id:
            return
        await self._drain_persistence()
        complete = (self._conversation_handoff_supported()
                    and provider_closed and not self._handoff_prior_unconfirmed
                    and self.conversation_mode == "active"
                    and self.server.conversations is not None
                    and self.persist_failures == 0 and self._persist_pending == 0)
        try:
            await self.ws.send(json.dumps({"type": "conversation_flushed",
                "session_id": self.session_id, "conversation_id": self.conversation_id,
                "status": "complete" if complete else "unknown",
                "provider_closed": provider_closed}))
        except Exception:  # A lost endpoint cannot acknowledge this boundary.
            pass

    def _provider_url(self) -> str:
        return f"{REALTIME_URL}?model={self.server.model}"

    async def _prepare_provider(self) -> None:
        self.open_stage = "handshake"
        while True:
            ev = json.loads(await asyncio.wait_for(self.oa.recv(), timeout=20))
            if ev.get("type") == "session.created":
                break
            if ev.get("type") == "error":
                raise RuntimeError(
                    "provider rejected the session: "
                    + sanitize_diagnostic(str(ev.get("error", {})), limit=240))
        self.open_stage = "configure"
        await self._configure()
        self.open_stage = "context"
        await self._attach_conversation()

    async def _close_provider(self, provider) -> None:
        await provider.close()

    def _provider_close_confirmed(self, provider) -> bool:
        return not getattr(getattr(self, "acceptance_guard", None), "forced", False)

    async def _return_tool_result(self, call_id: str, result: dict) -> None:
        await self.oa.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps(result, ensure_ascii=False)}}))

    async def _resume_after_tools(self) -> None:
        self.responding = True
        self.touch()
        await self.oa.send(json.dumps({"type": "response.create"}))

    async def _tool_call_current(self, turn_id: str) -> bool:
        return True

    async def _drain(self) -> None:
        """Die Tasks der vorigen Generation abwarten, nicht nur ihr Canceln anstossen.

        Ohne das gilt "abgeraeumt" nur als Absicht: close() ruft cancel() auf, aber die
        Tasks laufen bis zu ihrem naechsten await weiter. Die eigene Task wird ausgelassen —
        close() kann aus dem Reader (Silent-Stop) oder dem Timer (Timeout) heraus laufen,
        und auf sich selbst zu warten waere ein Deadlock.
        """
        pending = [t for t in self._draining if t is not asyncio.current_task()]
        self._draining = []
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def conversation_principal(self) -> str:
        """Der Principal, unter dem diese Sitzung ein Gespraech besitzt (§1.3 C3).

        AUSSCHLIESSLICH aus einem Sitzungsbeweis: `app_task_session.principal`
        (iPhone, nach bestandener App-Attest-Assertion) oder
        `browser_task_session.principal` (Browser, ohne den die Sitzung gar nicht
        oeffnet). `satellite_id` ist KEIN Principal — fuer das iPhone ist es der
        Anzeigename `iphone-…`, fuer den Pi die HMAC-Kennung. Ohne Beweis: leer.
        """
        app_proof = getattr(self, "app_task_session", None)
        browser_proof = getattr(self, "browser_task_session", None)
        principal = getattr(app_proof, "principal", "") if app_proof is not None else ""
        if not isinstance(principal, str) or not principal:
            principal = getattr(browser_proof, "principal", "") if browser_proof is not None else ""
        return principal if isinstance(principal, str) else ""

    def _open_conversation_context(self, store, bound: str, principal: str):
        room = getattr(self, "authenticated_room_device", "")
        owner = getattr(self.server, "room_history_owner", "")
        if room and owner and not bound and not principal:
            # Storage visibility, never a user/approval principal for this session.
            return store.begin_room_session(self.session_id, device_id=room, owner_principal=owner)
        if bound or principal:
            cid, resumed = store.begin_session(self.session_id, conversation_id=bound or None, principal=principal)
        else:
            cid, resumed = store.begin_session(self.session_id)
        return cid, resumed, store.recent_context(cid)

    async def _attach_conversation(self) -> None:
        """Diese Sprachsitzung an ein SOLVIO-Gespraech binden und den juengsten Ausschnitt
        als GESPRAECHSINHALT in die frische Provider-Sitzung einspielen.

        Die Historie geht ueber `conversation.item.create` — als Nachrichten mit den Rollen
        `user` und `assistant`. Sie wird bewusst NICHT in die Instructions geschrieben:
        frueherer Nutzertext ist Gespraech, keine Anweisung, und darf nicht zu einer
        Weisung hoeherer Autoritaet werden, nur weil er alt ist.

        An der echten API geprueft: Nutzer-Items brauchen `input_text`, Assistenten-Items
        `output_text` ("Value must be 'output_text'"). Ohne Einspielung antwortet das
        Modell, es kenne das Testwort nicht; mit Einspielung nennt es das Testwort.

        Versagt der Speicher, laeuft die Sitzung ZUSTANDSLOS weiter — degradiert, aber
        laut. Erfundener Kontext waere schlimmer als fehlender.
        """
        store = self.server.conversations
        bound = str(getattr(self, "bound_conversation_id", "") or "")
        if store is None:
            if bound:
                # Ein verlangter Chat ohne Speicher: kein degradierter Betrieb.
                self.conversation_mode = "refused"
                raise ConversationBindingError("conversation_not_bindable")
            self.conversation_mode = "off"
            return
        principal = self.conversation_principal()
        try:
            conversation_id, resumed, context = self._open_conversation_context(store, bound, principal)
        except ConversationStoreError as exc:
            if bound:
                # N8/C3: ein VERLANGTER Chat wird nicht degradiert, sondern verweigert —
                # sonst oeffnete die Sitzung ohne Chat, und nichts landete dort.
                self.conversation_mode = "refused"
                log.error("core.conversation_binding_refused", session_id=self.session_id,
                          conversation_id=bound, **describe_exception(exc))
                raise ConversationBindingError(str(exc)) from exc
            self.conversation_mode = "degraded"
            log.error("core.conversation_store_error", session_id=self.session_id,
                      stage="attach", conversation_mode="degraded", **describe_exception(exc))
            return
        self.conversation_id = conversation_id
        self.conversation_mode = "active"
        log.info("core.conversation_resumed" if resumed else "core.conversation_created",
                 session_id=self.session_id, conversation_id=conversation_id)
        if not context:
            return
        chars = 0
        for message in context:
            content_type = "input_text" if message["role"] == ROLE_USER else "output_text"
            await self.oa.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": message["role"],
                         "content": [{"type": content_type, "text": message["text"]}]}}))
            chars += len(message["text"])
        self.context_messages = len(context)
        self.context_chars = chars
        # Nur Umfang, nie Inhalt: der Gespraechstext gehoert nicht ins Betriebslog.
        log.info("core.conversation_context_loaded", session_id=self.session_id,
                 conversation_id=conversation_id, message_count=len(context),
                 context_chars=chars)
        await self._mention_proactive()

    # -- Die lange Recherche meldet sich zurueck -----------------------------
    def _watch_deep_result(self, name: str, result: dict) -> None:
        """Haengt einen Beobachter an eine Recherche, die weiterlaeuft.

        Der Beobachter haengt am PROZESS, nicht an dieser Sitzung: die
        schliesst nach `idle_timeout` (Vorgabe 30 s), eine Recherche dauert
        Minuten. Was diese Sitzung beisteuert, ist ein Zustellweg — und der
        prueft beim Zustellen selbst, ob es sie dann noch gibt.

        Drei Lagen, drei Handgriffe:

        * `deep_research` laeuft noch → beobachten.
        * `deep_task_status` sagt „laeuft noch" → derselbe Beobachter, aber
          DIESE Sitzung uebernimmt die Zustellung. Wer gerade nachfragt, hoert
          das Ergebnis auch.
        * `deep_cancel` → Beobachter beenden. Ein abgebrochener Auftrag meldet
          sich nicht mehr.
        """
        if name not in ("deep_research", "deep_task_status", "deep_cancel"):
            return
        followup = getattr(self.server, "deep_followup", None)
        if followup is None:
            return
        cancelled = _deep_task_cancelled(result)
        if cancelled:
            followup.stop(cancelled)
            return
        task_id = _deep_task_running(result)
        if not task_id:
            return
        if name == "deep_research":
            followup.watch(task_id, deliver=self.deliver_deep_note)
        else:
            followup.rebind(task_id, self.deliver_deep_note)

    async def deliver_deep_note(self, note: str) -> bool:
        """Sagt dem Menschen das Ergebnis — wenn diese Sitzung noch lebt.

        `False` heisst „nicht zugestellt"; der Beobachter legt die Meldung dann
        in den Eingang. Es gibt bewusst keinen Zwischenzustand: entweder der
        Mensch hoert es jetzt, oder er findet es spaeter.

        Als SYSTEMHINWEIS, nicht als Nutzertext — es ist eine Feststellung des
        Core und darf nicht wie eine Bitte des Menschen aussehen. Und der Text
        ist Executor-Ausgabe: er steht in einem Hinweis, nie in der Rolle einer
        Anweisung.
        """
        if not self._deliverable():
            return False
        try:
            await self.oa.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "system",
                         "content": [{"type": "input_text", "text": note}]}}))
            # `response.create` nur, wenn gerade keine Antwort laeuft: eine
            # zweite waehrend einer laufenden ist ein Protokollfehler. Ohne
            # sie steht der Hinweis trotzdem im Gespraech und wird im
            # naechsten Zug erzaehlt.
            if not self.responding:
                self.responding = True
                await self.oa.send(json.dumps({"type": "response.create"}))
            self.touch()
        except Exception as exc:  # noqa: BLE001 - eine Meldung kippt keine Sitzung
            log.info("deep.followup_send_failed", session_id=self.session_id,
                     kind=type(exc).__name__)
            return False
        log.info("deep.followup_spoken", session_id=self.session_id)
        return True

    def _deliverable(self) -> bool:
        """Lebt diese Sitzung noch so, dass sie sprechen kann?

        `active` allein genuegt nicht: `_on_provider_lost` setzt es waehrend
        eines Wiederverbindens auf falsch, obwohl die Sitzung weiterlebt. Was
        wirklich zaehlt, ist eine offene Anbieterverbindung und kein laufendes
        Schliessen.
        """
        return bool(self.active and not self._closing and not self._stopping
                    and self.oa is not None)

    async def _mention_proactive(self) -> None:
        """Sagen, DASS etwas da ist — nicht, was drinsteht.

        Nur, wenn der Endpunkt keine eigene Flaeche dafuer hat. Auf dem
        iPhone steht es im Tab „Hinweise", mit Zaehler; dort vorgelesen zu
        werden verlaengert ausgerechnet den ERSTEN Turn — den, an dem sich der
        Eindruck bildet — um Ton, den niemand angefordert hat.

        Waehrend niemand da war, kann der Hintergrund gearbeitet haben. Ohne
        diesen Hinweis erfaehrt der Nutzer davon nur, wenn er zufaellig fragt.

        Bewusst nur die ANZAHL und hoechstens drei Stichworte. Zwanzig Meldungen
        zu Beginn eines Gespraechs vorzulesen waere keine Aufmerksamkeit, sondern
        eine Zumutung — und die Inhalte holt sich das Modell ohnehin mit
        `proactive_list`, wenn der Mensch danach fragt.

        Als Systemhinweis, nicht als Nutzertext: es ist eine Feststellung des
        Core ueber seinen eigenen Zustand und darf nicht wie eine Bitte des
        Menschen aussehen.
        """
        if not self.mention_proactive:
            log.info("proactive.mention_skipped", session_id=self.session_id,
                     reason="endpoint_has_its_own_surface")
            return
        store = getattr(self.server.dispatcher, "proactive_store", None)
        if store is None:
            return
        try:
            count = await store.unread_count()
            if not count:
                return
            items = await store.unread(limit=3)
        except Exception as exc:  # noqa: BLE001 - ein Hinweis kippt keine Sitzung
            log.error("proactive.mention_failed", kind=type(exc).__name__)
            return
        headlines = "; ".join(str(i.get("zusammenfassung", ""))[:90] for i in items)
        # Der Wortlaut ist im Sprachtest teuer bezahlt worden. Vorher stand hier
        # „Erwaehne das knapp zu Beginn" — das Modell las es als Dauerauftrag
        # und begann JEDE Antwort mit „Ich habe 8 Sachen fuer dich aus dem
        # Hintergrund", auch auf „erzaehl mir etwas ueber Berlin". Die
        # eigentliche Frage wurde nie beantwortet. Ein Hinweis, der im Kontext
        # stehen bleibt, wird ohne ausdrueckliche Begrenzung zur stehenden
        # Pflicht.
        #
        # Deshalb: EINMAL, danach nie wieder. Die Begrenzung bleibt Wort fuer
        # Wort — nur die STELLE hat sich geaendert.
        #
        # Vorher stand hier „in deinem ersten Satz". Das Modell tat genau das,
        # und gemeldet wurde: „am Anfang kommt immer die Aussage, ich habe
        # 10 Sachen fuer dich, dann bricht es ab, und dann fangt er an, auf
        # meine Frage einzugehen." Wer eine App aufmacht und etwas fragt,
        # bekam zuerst eine Antwort auf etwas, das er nicht gefragt hatte.
        #
        # „Beantworte immer zuerst" und „erwaehne es im ersten Satz" waren ein
        # Widerspruch, und das Modell hat ihn so aufgeloest, wie ein Modell das
        # tut: die woertliche Anweisung schlug die allgemeine.
        note = (f"Systemhinweis (gilt EINMAL): waehrend der Abwesenheit sind "
                f"{count} Hintergrundmeldung(en) entstanden. Stichworte: "
                f"{headlines}. Beantworte ZUERST vollstaendig, was der Mensch "
                f"gerade gefragt hat. Haenge danach GENAU EINMAL einen kurzen "
                f"Nachsatz an (etwa 'Ich habe uebrigens {count} Sache(n) fuer "
                f"dich') und erwaehne es danach NIE WIEDER — auch nicht "
                f"angedeutet. Beginne KEINE Antwort damit. Einzelheiten zu den "
                f"Meldungen nur, wenn er danach fragt, dann ueber "
                f"proactive_list.")
        await self.oa.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system",
                     "content": [{"type": "input_text", "text": note}]}}))
        log.info("proactive.mentioned_at_session_start", unread=count)

    def _offer_memory_intent(self, transcript: str, *, message_id: str = ""):
        """Die erkannte Merk-Absicht dieses Turns hinterlegen — oder sie loeschen.

        Loeschen ist genauso wichtig wie Setzen: ohne das bliebe die Erlaubnis eines
        frueheren Turns bestehen, und ein spaeterer Werkzeugaufruf koennte sich darauf
        stuetzen, obwohl der Nutzer laengst ueber etwas anderes spricht.
        """
        gate = getattr(self.server.dispatcher, "memory_gate", None)
        if gate is None:
            return None
        detected = memory_intent.detect(transcript)
        gate.offer_user_turn(detected, conversation_id=self.conversation_id or "",
                             message_id=message_id, session_id=self.session_id,
                             turn_id=self._turn.get("turn_id", ""))
        if detected is not None:
            log.info("core.memory_intent_detected", session_id=self.session_id,
                     conversation_id=self.conversation_id, kind=detected.kind,
                     turn_id=self._turn.get("turn_id"))
        return detected

    def _offer_adaptive(self, transcript: str, *, message_id: str = "",
                        explicit: bool = False) -> None:
        """Diesen finalisierten Owner-Turn zum Lernen anbieten. NICHT blockierend.

        Sie steht bewusst NEBEN `_offer_memory_intent` und nicht darin: die
        ausdrueckliche Merk-Absicht ist ein Mandat mit Autoritaet, das Lernen
        ist eine Vermutung ohne. Beide aus derselben Zeile zu bedienen waere
        der erste Schritt, sie zu verwechseln.

        Zwei Bedingungen, beide strukturell:

        * `self.satellite_id` muss gesetzt sein — derselbe Beweis, aus dem
          `voice_trust()` `user_authorized` ableitet. Er belegt das GERAET.
        * Und `self.channel` muss eine Klasse tragen, die auch ueber den
          SPRECHER genug aussagt. Ein Raummikrofon tut das nicht; die Policy
          lehnt es mit `speaker_unverified` ab, und dieser Turn wird ganz
          normal weiterverarbeitet — nur eben ohne zu lernen.
        * Der Aufruf steht NACH den Stopp-Spuren und nach `_persist_message`,
          also nur auf einem Weg, den ein Stopp-Satz nie erreicht.

        Wirft nie. Lernen ist nachrangig; die Stimme haengt nicht daran.
        """
        # Jeder Ausstieg hier wird PROTOKOLLIERT. Eine stille Rueckkehr an einem
        # Pfad, der ueber Gedaechtnis entscheidet, ist nicht Sparsamkeit,
        # sondern Blindheit: bei der ersten Live-Abnahme lief ein Turn sauber
        # durch, es passierte nichts, und im Log stand kein einziger Hinweis
        # darauf, WO er verschwunden war.
        if explicit:
            # EIN TURN, EINE AUTORITAET. Hat der Mensch „merk dir" gesagt,
            # gehoert dieser Turn dem ausdruecklichen Weg — und der schreibt
            # `user_direct` mit voller Sicherheit.
            #
            # Live gemessen: ohne diese Zeile entstanden aus „Merk dir
            # dauerhaft, meine Lieblingsfarbe ist Petrol" ZWEI Eintraege — der
            # ausdrueckliche und ein gelernter „Seine Lieblingsfarbe ist
            # Petrol." Die Entdopplung greift nicht, weil der Extraktor in die
            # dritte Person umschreibt und der Schluessel damit ein anderer
            # ist. Zwei aktive Wahrheiten ueber dasselbe, mit verschiedener
            # Vertrauensstufe — und die schwaechere haette ein Vergessen der
            # staerkeren ueberlebt.
            log.info("adaptive.skipped_explicit_turn",
                     session_id=self.session_id)
            return
        adaptive = getattr(self.server.dispatcher, "adaptive_memory", None)
        if adaptive is None:
            log.info("adaptive.not_wired", session_id=self.session_id)
            return
        if not (transcript or "").strip():
            log.info("adaptive.empty_transcript", session_id=self.session_id)
            return
        if not self.satellite_id:
            log.info("adaptive.unproven_device", session_id=self.session_id,
                     channel=self.channel)
            return
        try:
            from solvio.memory.adaptive.policy import OwnerTurn
            observations = getattr(self.server.dispatcher, "memory_observations", None)
            if observations is None:
                log.info("adaptive.cost_context_missing", session_id=self.session_id)
                return
            turn = OwnerTurn(
                text=transcript, channel=self.channel, role="user",
                conversation_id=self.conversation_id or "",
                session_id=self.session_id,
                turn_id=self._turn.get("turn_id", ""),
                message_id=message_id)
            store = self.server.conversations

            async def persisted():
                # _persist_message enqueues; its returned ID alone is not yet
                # a storage receipt. Wait outside the realtime reader and
                # compare the actual canonical message before extraction.
                if store is None:
                    return False
                try:
                    await asyncio.wait_for(self._persist_queue.join(), timeout=2.0)
                    row = await asyncio.to_thread(store.message, turn.conversation_id, turn.message_id)
                    return row is not None and all(row.get(key) == value for key, value in {
                        "text": turn.text.strip(), "role": turn.role,
                        "source_session_id": turn.session_id, "source_turn_id": turn.turn_id}.items())
                except Exception:
                    return False

            proof = (getattr(self, "browser_task_session", None) if self.channel == "voice_browser"
                     else getattr(self, "app_task_session", None))
            accepted = observations.offer_voice(turn, proof,
                                                persisted=persisted)
            log.info("adaptive.offered", session_id=self.session_id,
                     channel=self.channel, accepted=accepted)
        except Exception as exc:  # noqa: BLE001 - Lernen wirft nie nach oben
            log.info("adaptive.offer_failed", kind=type(exc).__name__,
                     detail=str(exc)[:200])

    def _offer_shadow(self, *, observed_kind: str, tools: tuple[str, ...] = (),
                      approval: bool = False, turn_id: str = "") -> None:
        """Diesen finalisierten Turn zur MESSUNG anbieten. Ohne jede Wirkung.

        Sie steht bewusst NEBEN `_offer_adaptive` und nach demselben Muster:
        nach dem echten Ergebnis, nie im Antwortpfad, wirft nie. Der
        Unterschied ist der Zweck — das Lernen ist eine Vermutung ueber den
        Menschen, das hier ist eine Messung ueber SOLVIO selbst.

        **Nur im Schattenmodus.** In `off` existiert nichts davon, in `active`
        ist der Router der Handelnde und es gibt nichts zu vergleichen.

        Sie beauftragt nichts, sie fordert keine Freigabe an, sie beruehrt
        keine Autoritaet und sie kostet den Turn null Millisekunden: das
        Ergebnis steht laengst, wenn sie startet.
        """
        router = getattr(self.server.dispatcher, "cognition", None)
        if router is None or getattr(router, "mode", "off") != "shadow":
            return
        text = (self.turn_user_text or "").strip()
        if not text:
            return
        gemessen = self._shadow_seen
        if turn_id in gemessen:
            # Ein Turn, eine Zeile. Eine zweite Antwortrunde auf dieselbe
            # Aeusserung ist kein zweiter Fall.
            return
        gemessen.add(turn_id)
        try:
            import asyncio as _asyncio

            from solvio.capabilities.policy import origin_for_session
            herkunft = origin_for_session(
                self.channel,
                interactive_proof=bool(getattr(self, "interactive_proof", False)),
                browser_task_session=getattr(self, "browser_task_session", None),
                session_id=self.session_id)
            aufgabe = _asyncio.ensure_future(router.observe_turn(
                user_text=text, conversation_ref=self.conversation_id or "",
                turn_ref=turn_id, origin=getattr(herkunft, "value", ""),
                observed_kind=observed_kind,
                observed_tool=",".join(tools), observed_approval=approval))
            router._shadow_tasks.add(aufgabe)
            aufgabe.add_done_callback(router._shadow_tasks.discard)
        except Exception as exc:  # noqa: BLE001 - eine Messung wirft nie nach oben
            log.info("cognition.observe_failed", kind=type(exc).__name__)

    def _persist_message(self, role: str, text: str) -> str:
        """Eine finalisierte Nachricht zum Schreiben uebergeben — nicht hier schreiben.

        Der Reader haelt nicht an. Die Reihenfolge bleibt erhalten, weil genau ein Worker
        die Warteschlange abarbeitet.

        Die Kennung wird HIER vergeben und zurueckgegeben: die Merk-Absicht muss sich auf
        die Nachricht beziehen koennen, aus der sie stammt, waehrend der Schreibvorgang
        noch in der Warteschlange liegt.
        """
        if (self.server.conversations is None or self.conversation_id is None
                or self.conversation_mode != "active"):
            return ""
        if not (text or "").strip():
            return ""
        message_id = "m-" + secrets.token_hex(8)
        self._persist_pending += 1
        self._persist_queue.put_nowait((role, text, self._turn.get("turn_id"), message_id))
        return message_id

    async def _persist_loop(self) -> None:
        """Der einzige Schreiber. SQLite laeuft im Thread, damit der Event-Loop frei bleibt."""
        store = self.server.conversations
        try:
            while True:
                role, text, turn_id, message_id = await self._persist_queue.get()
                try:
                    message_id = await asyncio.to_thread(
                        store.add_message, self.conversation_id, role, text,
                        source_session_id=self.session_id, source_turn_id=turn_id,
                        message_id=message_id)
                    if message_id:
                        log.info("core.conversation_message_persisted",
                                 session_id=self.session_id,
                                 conversation_id=self.conversation_id, role=role,
                                 turn_id=turn_id, chars=len(text.strip()))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Ein Speicherfehler macht diese Sitzung zustandslos — er darf sie nicht
                    # abwuergen und darf spaetere Nachrichten nicht als gespeichert ausgeben.
                    self.persist_failures += 1
                    self.conversation_mode = "degraded"
                    log.error("core.conversation_store_error", session_id=self.session_id,
                              conversation_id=self.conversation_id, stage="persist",
                              role=role, conversation_mode="degraded",
                              **describe_exception(exc))
                finally:
                    self._persist_pending -= 1
                    self._persist_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _drain_persistence(self, timeout: float = 5.0) -> int:
        """Offene Schreibvorgaenge abschliessen lassen, bevor die Sitzung endet.

        Die letzte Aeusserung eines Gespraechs ist die, an die sich der Nutzer als naechstes
        erinnert sehen will — sie darf nicht verloren gehen, nur weil der Timeout zuschlug.
        Nach `timeout` wird abgebrochen und die Zahl der unerledigten Schreibvorgaenge
        gemeldet, statt sie stillschweigend zu verlieren.
        """
        # NICHT auf queue.empty() pruefen: eine bereits entnommene, noch laufende
        # Schreibaufgabe laesst die Warteschlange leer aussehen, waehrend join() zu Recht
        # noch wartet. Genau dieser Fall — ein haengender Schreiber — war der interessante.
        if self._persist_worker is None or self._persist_pending == 0:
            return 0
        try:
            await asyncio.wait_for(self._persist_queue.join(), timeout=timeout)
            return 0
        except asyncio.TimeoutError:
            pending = self._persist_pending
            log.error("core.conversation_store_error", session_id=self.session_id,
                      conversation_id=self.conversation_id, stage="drain",
                      unwritten_messages=pending)
            return pending

    async def _configure(self) -> None:
        tools = self.server.dispatcher.openai_tools() if self.server.dispatcher else []
        # Ein Werkzeug, das der Core selbst bedient und der Dispatcher nie sieht.
        #
        # Der Grund steht in fuenf Zeilen Log: derselbe gesprochene Name kam als
        # „solve your", „zovi", „olwe", „fang ich" und „halt er" an. Auf Worte
        # zu warten, die so schwanken, ist aussichtslos — und die Regeln weit
        # genug zu machen hiess, „alles aus" und „mach stop" zu
        # Gespraechsenden zu machen. Das hat das Gate bereits einmal gefangen.
        #
        # Das MODELL versteht die Bitte dagegen zuverlaessig: es antwortete
        # „alles klar, ich stoppe jetzt" — es hatte die Absicht erkannt und nur
        # kein Mittel, sie auszufuehren. Also redete es darueber. Hier ist das
        # Mittel.
        #
        # Das ist keine Autoritaet: ein Gespraech zu beenden hat keine Wirkung
        # in der Welt, und der Core fuehrt es aus, nicht das Modell.
        tools = list(tools) + [END_CONVERSATION_TOOL]
        session = {
            "type": "realtime",
            "output_modalities": ["audio"],
            # Der Modus haengt am prozessweiten Dispatcher und wird HIER
            # gelesen, nicht gemerkt: eine Sitzung, die spaeter oeffnet, soll
            # die Anweisung bekommen, die dann gilt.
            "instructions": SYSTEM_INSTRUCTIONS + (
                tool_instructions_for(
                    getattr(self.server.dispatcher, "cognitive_router_mode", "off"))
                if tools else ""),
            "tools": tools,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "transcription": {"model": TRANSCRIPTION_MODEL,
                                      "language": TRANSCRIPTION_LANGUAGE},
                    # Die Bereitwilligkeit gilt PRO SITZUNG, nicht pro Server.
                    #
                    # Ein Satellit steht fest in einem ruhigen Raum; ein Telefon
                    # liegt in der Hand, und daneben laeuft ein Fernseher. Mit
                    # `high` haelt SOLVIO dort bei jedem Geraeusch an. Wer
                    # nichts setzt, bekommt weiterhin genau den Wert des
                    # Servers — der Pi-Pfad ist damit unveraendert.
                    "turn_detection": {"type": "semantic_vad",
                                       "eagerness": self.eagerness or self.server.eagerness,
                                       "create_response": True, "interrupt_response": True},
                },
                "output": {"format": {"type": "audio/pcm", "rate": OPENAI_RATE}, "voice": self.server.voice},
            },
        }
        await self.oa.send(json.dumps({"type": "session.update", "session": session}))
        await self.oa.send(json.dumps({"type": "session.update",
                                       "session": {"type": "realtime", "reasoning": {"effort": self.server.effort}}}))

    def begin_open(self) -> None:
        """Die Anbietersitzung nebenher oeffnen.

        Der Aufrufer ist die Nachrichtenschleife des Satelliten, und die muss
        weiterlaufen: waehrend der Oeffnung spricht der Mensch bereits. Ein
        Fehlschlag wird HIER behandelt und nicht dem Aufrufer ueberlassen —
        eine Task, deren Ausnahme niemand abholt, endet als stille Warnung im
        Ereignisprotokoll, und der Satellit bliebe in ACTIVE haengen, ohne je
        ein `session_end` zu sehen.
        """
        if self._open_task is not None and not self._open_task.done():
            log.warning("core.session_start_ignored", session_id=self.session_id,
                        reason="already_opening")
            return
        log.info("voice.session_opening", session_id=self.session_id,
                 satellite_id=self.satellite_id or "")
        self._open_task = asyncio.create_task(self._open_guarded())

    async def _open_guarded(self) -> None:
        try:
            await self.open()
        except asyncio.CancelledError:
            # Abgebrochen, weil die Sitzung inzwischen schliesst — meist, weil
            # der Satellit aufgelegt hat, bevor der Anbieter antwortete. Das
            # ist kein Fehlschlag, aber es soll diagnostizierbar bleiben:
            # sonst verschwindet ein langsamer Verbindungsaufbau spurlos.
            log.info("core.session_open_abandoned", session_id=self.session_id,
                     stage=self.open_stage, attempt=self.open_attempts)
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("core.session_open_failed", stage=self.open_stage,
                      attempt=self.open_attempts, model=self.server.model,
                      **describe_exception(exc))
            await self.close(reason="open_failed")

    async def feed_audio(self, pcm16: bytes) -> None:
        """Audio vom Satelliten weiterreichen — oder halten, bis es geht.

        Der frueher hier stehende `return` war der Klippdefekt: solange der
        Anbieter noch nicht antwortete, verschwand jedes Frame lautlos. Gehalten
        wird jetzt, nicht verworfen.
        """
        self._frames_in += 1
        self._observe_audio("received")
        if self.t_first_frame is None:
            self.t_first_frame = time.monotonic()
        if not self.active or self.oa is None:
            self._frames_before_ready += 1
            self.preroll.add(pcm16)
            return
        # Schonfrist am Anfang einer Antwort: STILLE statt Mikrofon, damit der
        # auslaufende Satz des Menschen nicht als neue Runde gilt. Der Strom
        # reisst nicht ab — es geht Stille hinaus, nicht nichts.
        if self._within_utterance_guard():
            self._guarded_frames += 1
            await self._to_provider(bytes(len(pcm16)))
            return
        await self._to_provider(pcm16)

    def _within_utterance_guard(self) -> bool:
        """Hat die laufende Antwort gerade erst begonnen?"""
        if not self.responding or not self._turn:
            return False
        began = self._turn.get("first_audio_sent")
        if began is None:
            return False
        return (time.monotonic() - began) < UTTERANCE_GUARD_SECONDS

    async def _to_provider(self, pcm16: bytes) -> None:
        audio_generation = self.open_attempts
        provider = self.oa
        pcm24 = self.up.process(pcm16)
        if pcm24:
            await provider.send(json.dumps({"type": "input_audio_buffer.append",
                                           "audio": base64.b64encode(pcm24).decode("ascii")}))
            self._observe_audio("forwarded", audio_generation)

    def _audio_wanted(self, ev: dict) -> bool:
        """Gehoert dieses Audio noch zur laufenden Antwort?

        Nach einem Barge-in ist das Tor zu, bis der Anbieter eine NEUE Antwort
        beginnt. Zusaetzlich wird die Kennung der abgebrochenen Antwort
        gefuehrt: ein spaetes Delta, das erst nach `response.created` der
        naechsten Antwort eintrudelt, faende das Tor sonst wieder offen.

        Fehlt die Kennung im Ereignis, entscheidet allein das Tor. Das ist die
        vorsichtige Richtung: lieber ein Frame zu wenig als die alte Stimme
        ueber der neuen Frage.
        """
        response_id = str(ev.get("response_id", "") or "")
        if response_id and response_id in self._dead_responses:
            return False
        return self._audio_open

    async def _barge_in(self, played_ms: int | None = None) -> None:
        """Der Mensch faengt an zu reden, waehrend SOLVIO spricht.

        `played_ms` ist, was der Endpunkt tatsaechlich abgespielt hat, bevor er
        still wurde — eine Beobachtung des Geraets, keine Entscheidung. Was sie
        bedeutet, entscheidet hier der Core.

        Vier Dinge, in dieser Reihenfolge, weil jede spaetere Zeile Zeit kostet
        und der Mensch die Stille sofort hoeren soll:

        1. Das Tor zu — ab hier wird kein Frame der alten Antwort mehr
           weitergereicht, ganz gleich, was noch eintrudelt.
        2. Den Satelliten leeren — was dort in der Warteschlange liegt, ist
           bereits gesendet und wuerde sonst noch zu Ende gespielt.
        3. Den Anbieter abbrechen. `semantic_vad` tut das mit
           `interrupt_response` ohnehin selbst; die ausdrueckliche Bitte ist
           die guenstigere Doppelung, weil sie nicht davon abhaengt, dass die
           Erkennung des Anbieters und unsere denselben Moment meinen.
        """
        self._audio_open = False
        if self._response_id:
            self._dead_responses.append(self._response_id)
        await self.ws.send(json.dumps({"type": "flush"}))
        log.info("voice.barge_in", session_id=self.session_id,
                 turn_id=self._turn.get("turn_id"))
        try:
            await self.oa.send(json.dumps({"type": "response.cancel"}))
        except Exception as exc:  # noqa: BLE001 - ein Abbruch, der nicht ankommt, darf nicht die Sitzung kippen
            log.info("voice.cancel_failed", session_id=self.session_id,
                     kind=type(exc).__name__)
            return
        log.info("voice.assistant_audio_cancelled", session_id=self.session_id,
                 turn_id=self._turn.get("turn_id"))
        await self._truncate_heard(played_ms)

    async def _truncate_heard(self, played_ms: int | None) -> None:
        """Sagt dem Anbieter, wie weit der Mensch gekommen ist.

        Ohne das bleibt im Gedaechtnis des Modells der GANZE Satz stehen, auch
        der Teil, den niemand gehoert hat. Es antwortet danach auf etwas, das
        es nur gedacht hat — und genau daran fuehlt sich ein unterbrochenes
        Gespraech unnatuerlich an. Ein Mensch weiss, wo man ihm ins Wort
        gefallen ist.

        Fehlt die Angabe oder kennen wir den Gegenstand nicht, passiert nichts:
        eine geratene Zahl waere schlimmer als keine.
        """
        if played_ms is None or played_ms < 0 or not self._audio_item:
            return
        try:
            await self.oa.send(json.dumps({
                "type": "conversation.item.truncate",
                "item_id": self._audio_item,
                "content_index": self._audio_index,
                "audio_end_ms": int(played_ms)}))
            log.info("voice.truncated_to_heard", session_id=self.session_id,
                     played_ms=int(played_ms))
        except Exception as exc:  # noqa: BLE001
            log.info("voice.truncate_failed", session_id=self.session_id,
                     kind=type(exc).__name__)

    async def note_barge_in(self, played_ms: Any = None) -> None:
        """Ein Endpunkt meldet, dass er den Menschen gehoert hat und still ist.

        Der Core tut daraufhin genau, was er auch bei der Erkennung des
        Anbieters tut. Der Unterschied ist nur, WER es zuerst bemerkt hat —
        nicht, wer entscheidet.
        """
        try:
            heard = int(played_ms) if played_ms is not None else None
        except (TypeError, ValueError):
            heard = None
        log.info("core.ENDPOINT_BARGE_IN", session_id=self.session_id,
                 played_ms=heard)
        await self._barge_in(heard)

    async def note_heard(self, played_ms: Any = None) -> None:
        """Wie viel der Mensch von der abgebrochenen Antwort wirklich gehoert hat.

        Nur die Wahrheit nachreichen, nichts abbrechen: unterbrochen wurde
        schon.
        """
        try:
            heard = int(played_ms) if played_ms is not None else None
        except (TypeError, ValueError):
            return
        await self._truncate_heard(heard)

    async def _flush_preroll(self) -> None:
        """Den gehaltenen Satzanfang abgeben, VOR jedem lebenden Frame.

        Genau einmal je gehaltenem Abschnitt, und das garantiert `drain()`
        selbst, indem es beim Auslesen leert. Hier stand kurz eine zusaetzliche
        Marke „schon abgeflossen"; sie war nicht nur ueberfluessig, sondern
        schaedlich: nach einem Verbindungsabbruch laeuft wieder etwas in den
        Puffer, und beim Wiederverbinden haette die Marke genau dieses Audio
        fuer immer liegen lassen. Eine Mutation hat das gezeigt — der Test
        ueber sie blieb gruen, obwohl sie entfernt war.
        """
        stats = self.preroll.stats()
        held = self.preroll.drain()
        if not held:
            return
        await self._to_provider(held)
        log.info("voice.preroll_flushed", session_id=self.session_id, **stats)

    def _begin_turn(self) -> None:
        """Ein Turn beginnt, wenn der Nutzer zu sprechen anfaengt."""
        self.turn_no += 1
        self.turn_user_text = ""
        # Ab hier ist der Nutzertext dieses Turns UNBEKANNT — nicht leer.
        # Der Unterschied entscheidet ueber eine Freigabefrage: „er hat nichts
        # gesagt" und „er hat gesprochen, das Transkript ist nur noch nicht da"
        # duerfen nicht dasselbe bedeuten.
        self.turn_text_ready = asyncio.Event()
        self._turn = {"turn_id": f"{self.session_id}-t{self.turn_no}",
                      "speech_started": time.monotonic()}

    async def _await_turn_text(self) -> bool:
        """Wartet kurz darauf, dass der Nutzertext dieses Turns feststeht.

        Fail-closed und knapp bemessen: kommt in dieser Zeit nichts, wird nicht
        angenommen, der Nutzer habe etwas beauftragt. Die Faehigkeit laeuft dann
        ueber den Freigabeweg — dasselbe, was vor Approval Policy V2 galt.
        """
        if self.turn_text_ready.is_set():
            return True
        if not (self._turn or {}).get("speech_started"):
            # In diesem Turn hat niemand gesprochen — eine Modellrunde, ein
            # fortgesetzter Werkzeugaufruf. Da ist kein Transkript unterwegs,
            # und darauf zu warten waere zwei Sekunden Stillstand fuer nichts.
            #
            # Genau das ist beim Bau passiert: die Wartezeit galt jedem
            # Werkzeugaufruf, und eine Zusicherung, die misst, ob Werkzeuge
            # NEBEN dem Leseweg laufen, fiel sofort darueber. Gewartet wird nur,
            # wo tatsaechlich etwas ankommt.
            return False
        try:
            await asyncio.wait_for(self.turn_text_ready.wait(),
                                   timeout=TURN_TEXT_WAIT_SECONDS)
            return True
        except asyncio.TimeoutError:
            log.info("core.turn_text_not_ready", session_id=self.session_id,
                     waited_s=TURN_TEXT_WAIT_SECONDS)
            return False

    def _mark(self, key: str) -> None:
        """Ersten Zeitpunkt eines Turn-Ereignisses festhalten. Spaetere ueberschreiben nicht."""
        if self._turn and key not in self._turn:
            self._turn[key] = time.monotonic()

    @staticmethod
    def _ms(t0, t1):
        """Dauer in ms — oder None, wenn einer der Punkte nicht ehrlich messbar war."""
        if t0 is None or t1 is None:
            return None
        return int((t1 - t0) * 1000)

    def _emit_turn(self) -> None:
        """Ein Turn ist fertig: die abgeleiteten Dauern in EINER Zeile.

        Fehlt ein Zeitpunkt, bleibt die abgeleitete Zahl `None` statt geschaetzt zu werden —
        eine erfundene Latenz waere schlimmer als eine fehlende.
        """
        t = self._turn
        if not t:
            return
        fields = {
            "session_id": self.session_id, "turn_id": t.get("turn_id"),
            "speech_end_to_response_start_ms": self._ms(t.get("speech_ended"),
                                                        t.get("response_started")),
            "speech_end_to_first_audio_ms": self._ms(t.get("speech_ended"),
                                                     t.get("first_audio_recv")),
            "provider_first_audio_to_satellite_send_ms": self._ms(t.get("first_audio_recv"),
                                                                  t.get("first_audio_sent")),
            "transcript_ready_ms": self._ms(t.get("speech_ended"), t.get("transcript_ready")),
            "turn_total_ms": self._ms(t.get("speech_started"), t.get("response_done")),
            "tool_calls": t.get("tool_calls", 0),
        }
        log.info("core.turn_latency", **{k: v for k, v in fields.items() if v is not None})
        self._turn = {}

    def _provider_pcm(self, delta: str) -> bytes:
        """Provider-Audio in Satelliten-PCM wandeln — oder nichts, wenn das Frame kaputt ist.

        Reproduziert: ein Delta, das zu einer UNGERADEN Byte-Zahl dekodiert, laesst
        array.frombytes in Resampler.process werfen. Die Ausnahme lief bis in _oa_reader,
        beendete den Reader und damit still die ganze Sitzung: das naechste GUTE Audio-Frame
        kam nicht mehr an, und response.done wurde nicht mehr verarbeitet.

        Ein kaputtes Frame wird VERWORFEN, nicht repariert. Ein halbes Sample zu ergaenzen
        hiesse, Audio zu erfinden — und ein halbes Sample weiterzureichen hiesse, dem
        Satelliten einen um ein Byte verschobenen Strom zu schicken. Protokolliert werden
        Kategorie und Laenge, niemals die Nutzdaten.
        """
        try:
            raw = base64.b64decode(delta or "", validate=True)
        except (binascii.Error, ValueError):
            self._drop_audio("undecodable_base64")
            return b""
        if len(raw) % 2:
            self._drop_audio("odd_byte_count", len(raw))
            return b""
        try:
            return self.down.process(raw)
        except (ValueError, ArithmeticError, IndexError):
            self._drop_audio("pcm_conversion_failed", len(raw))
            return b""

    def _drop_audio(self, reason: str, byte_count: int | None = None) -> None:
        """Ein verworfenes Frame kategorisch melden — ohne die Nutzdaten und ohne Flut.

        Nur die ersten Meldungen gehen ins Log; ein Provider, der dauerhaft Unsinn sendet,
        soll es nicht fluten. Die Gesamtzahl steht am Sitzungsende in core.session_closing,
        damit nichts unbemerkt verschwindet.
        """
        self.dropped_audio_frames += 1
        if self.dropped_audio_frames <= _MAX_AUDIO_DROP_LOGS:
            log.warning("core.audio_frame_dropped", session_id=self.session_id,
                        reason=reason, byte_count=byte_count,
                        dropped_so_far=self.dropped_audio_frames)

    async def _oa_reader(self, *, audio_generation: int | None = None) -> None:
        audio_generation = self.open_attempts if audio_generation is None else audio_generation
        try:
            async for raw in self.oa:
                ev = json.loads(raw)
                et = ev.get("type", "")
                if et in ("response.output_audio.delta", "response.audio.delta"):
                    # Die Marke gehoert HINTER das Tor. Davor stempelte ein
                    # Delta der abgebrochenen Antwort den NEUEN Turn: es kommt
                    # an, bevor dessen `response.created` da ist, und die
                    # abgeleitete Dauer wurde dadurch negativ (gemessen: -5 ms).
                    # Eine erfundene Latenz ist schlimmer als eine fehlende.
                    if not self._stopping and self._audio_wanted(ev):
                        self._audio_item = str(ev.get("item_id", "") or "")
                        self._audio_index = int(ev.get("content_index", 0) or 0)
                        self._mark("first_audio_recv")
                        pcm16 = self._provider_pcm(ev.get("delta", ""))
                        if pcm16:
                            first = self._turn and "first_audio_sent" not in self._turn
                            # **Der zweite Messpunkt, und er fehlte.**
                            #
                            # Laeuft in einem Turn ein Werkzeug, hoert der
                            # Mensch ZWEIMAL: erst die Vorbemerkung („einen
                            # Moment"), dann die eigentliche Antwort. Bisher
                            # wurde nur die erste protokolliert — die Frage
                            # „wie lange bis zur Antwort?" war deshalb aus den
                            # Daten nicht zu beantworten (gemessen am
                            # Sprachtest vom 07.09.2026: genau EIN
                            # `assistant_audio_started` fuer einen Turn mit
                            # 10,2 s Werkzeugaufruf).
                            #
                            # Erkannt wird die Antwort daran, dass in diesem
                            # Turn bereits ein Werkzeug lief — die Vorbemerkung
                            # geht dem Werkzeugaufruf voraus.
                            antwort = (self._turn
                                       and self._turn.get("tool_calls", 0) > 0
                                       and "answer_audio_sent" not in self._turn)
                            await self.ws.send(pcm16)
                            self._mark("first_audio_sent")
                            if first:
                                log.info("voice.assistant_audio_started",
                                         session_id=self.session_id,
                                         turn_id=self._turn.get("turn_id"))
                            if antwort:
                                self._mark("answer_audio_sent")
                                log.info(
                                    "voice.assistant_answer_audio_started",
                                    session_id=self.session_id,
                                    turn_id=self._turn.get("turn_id"),
                                    speech_end_to_answer_audio_ms=self._ms(
                                        self._turn.get("speech_ended"),
                                        self._turn.get("answer_audio_sent")),
                                    preamble_to_answer_audio_ms=self._ms(
                                        self._turn.get("first_audio_sent"),
                                        self._turn.get("answer_audio_sent")))
                    self.touch()
                elif et == "input_audio_buffer.speech_started":
                    was_responding = self.responding
                    self.speaking = True
                    self.touch()
                    self._begin_turn()
                    log.info("core.USER_SPEECH_STARTED", session_id=self.session_id,
                             turn_id=self._turn.get("turn_id"))
                    if was_responding:
                        await self._barge_in()
                    else:
                        await self.ws.send(json.dumps({"type": "flush"}))
                elif et == "input_audio_buffer.speech_stopped":
                    self.speaking = False
                    self.touch()
                    self._mark("speech_ended")
                    log.info("core.USER_SPEECH_ENDED", session_id=self.session_id,
                             turn_id=self._turn.get("turn_id"))
                elif et == "conversation.item.input_audio_transcription.completed":
                    self._mark("transcript_ready")
                    transcript = ev.get("transcript", "")
                    # Die Kontrollspur zuerst. Sie gehoert dem Core, nicht dem
                    # Modell, und sie laeuft vor JEDER Zustellung — auch vor
                    # dem Persistieren und vor jedem Werkzeugweg.
                    if is_conversation_stop(transcript):
                        log.info("core.CONVERSATION_STOP", session_id=self.session_id,
                                 lane="deterministic")
                        await self._silent_stop()
                        return
                    if is_silent_stop(transcript):
                        await self._silent_stop()
                        return
                    # M1: erst jetzt ist der Nutzertext endgueltig — vorher war er nur ein
                    # Zwischenstand. Ein Stopp-Satz wird nicht Teil des Gespraechs.
                    message_id = self._persist_message(ROLE_USER, transcript)
                    self.turn_user_text = transcript
                    self.turn_text_ready.set()
                    # M2: und erst jetzt steht fest, ob DIESE Aeusserung eine
                    # ausdrueckliche Merk-Absicht trug. Die Entscheidung faellt hier,
                    # aus dem Nutzertranskript — nicht im Modell und nicht aus einer
                    # Werkzeugausgabe. Das Modell sieht sie nur als Erlaubnis.
                    detected = self._offer_memory_intent(transcript,
                                                          message_id=message_id)
                    # Und danach das Lernen — eine Vermutung ohne Autoritaet,
                    # asynchron, nie im Antwortpfad. Ein Turn mit
                    # ausdruecklicher Merk-Absicht gehoert nicht ihm.
                    self._offer_adaptive(transcript, message_id=message_id,
                                         explicit=detected is not None)
                    self.touch()
                elif et == "conversation.item.input_audio_transcription.failed":
                    # Fail-closed: ohne finalisiertes Transkript gibt es fuer diesen Turn
                    # keine Merk-Absicht. Ohne diesen Zweig blieb das Mandat des VORIGEN
                    # Turns scharf, und ein Werkzeugaufruf haette es einloesen koennen.
                    log.warning("core.transcription_failed", session_id=self.session_id,
                                turn_id=self._turn.get("turn_id"))
                    # Ohne Transkript ist nichts als „vom Nutzer gesagt" belegbar.
                    self.turn_user_text = ""
                    self.turn_text_ready.set()
                    self._offer_memory_intent("")
                    self.touch()
                elif et == "response.created":
                    self.responding = True
                    # Eine neue Antwort darf sprechen. Die alte bleibt tot —
                    # dafuer steht ihre Kennung auf der Liste.
                    self._response_id = str((ev.get("response") or {}).get("id", ""))
                    self._audio_open = True
                    self.touch()
                    self._mark("response_started")
                    log.info("core.SOLVIO_RESPONSE_STARTED", session_id=self.session_id,
                             turn_id=self._turn.get("turn_id"))
                elif et in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                    txt = ev.get("transcript", "")
                    if txt:
                        # Frueher stand hier der volle Satz. Das Protokoll ist die
                        # falsche Ablage dafuer: es wird rotiert, kopiert, in
                        # Fehlerberichte gehaengt und von Werkzeugen gelesen, die
                        # nichts mit dem Gespraech zu tun haben. Der Wortlaut
                        # gehoert in den Gespraechsspeicher — dort steht er unter
                        # bekannten Rechten und mit einer Loeschgeschichte. Hier
                        # bleibt, was zum Betrieb noetig ist: dass geantwortet
                        # wurde und wie lang.
                        log.info("core.assistant_utterance", session_id=self.session_id,
                                 turn_id=self._turn.get("turn_id"), chars=len(txt.strip()))
                        self._persist_message(ROLE_ASSISTANT, txt)
                elif et == "response.done":
                    # Das `response.done` der ABGEBROCHENEN Antwort traf bisher
                    # den neuen Turn: es stempelte `response_done` auf ihn, gab
                    # ihn aus — `turn_total_ms=2`, gemessen von SEINEM
                    # Sprechbeginn bis zum Ende der VORIGEN Antwort — und
                    # leerte ihn. Danach war jede weitere Marke wirkungslos
                    # (`_mark` prueft `if self._turn`), und jede Folgezeile trug
                    # `turn_id=None`. Der echte Turn bekam nie eine Zeile.
                    done_id = str((ev.get("response") or {}).get("id", ""))
                    # Gehoert dieses Ende zu dem Turn, der gerade offen ist?
                    #
                    # Zwei Faelle sagen nein. Der erste ist die abgebrochene
                    # Antwort. Der zweite wurde erst im Sprachtest sichtbar:
                    # faengt der Mensch an zu reden, waehrend die vorige Antwort
                    # ausklingt, beginnt ein neuer Turn — und deren `done`
                    # stempelte ihn ab (`turn_total_ms=0`) und leerte ihn,
                    # wonach jede Folgezeile `turn_id=None` trug. Ein Turn ohne
                    # eigene `response.created` kann nicht das Ende einer
                    # Antwort sein.
                    #
                    # Die Unterscheidung betrifft AUSSCHLIESSLICH die Messung.
                    # Werkzeugaufrufe laufen in jedem Fall: sie gehoeren zur
                    # Antwort, nicht zum Turn, und sie zu verschlucken hiesse,
                    # eine erbetene Handlung zu unterschlagen. Eine erste
                    # Fassung dieser Wache tat genau das — zwei bestehende
                    # Tests haben es sofort gefunden.
                    owns_turn = (done_id not in self._dead_responses
                                 and (not self._turn
                                      or "response_started" in self._turn))
                    self.responding = False
                    self.touch()
                    outputs = (ev.get("response", {}) or {}).get("output", []) or []
                    calls = [o for o in outputs if o.get("type") == "function_call"]
                    if not calls and owns_turn:
                        # DER FALL, DEN DIE ALTE MESSUNG NICHT SAH: das Modell
                        # hat selbst geantwortet. Genau hier steckt „verpasste
                        # Arbeit", und genau hier war der Schatten bisher blind.
                        # Nur ein `_offer_*` wie beim Lernen — der Reader haelt
                        # nicht an.
                        self._offer_shadow(observed_kind="direkt",
                                           turn_id=self._turn.get("turn_id", ""))
                    if calls:
                        if owns_turn:
                            self._turn["tool_calls"] = \
                                self._turn.get("tool_calls", 0) + len(calls)
                        # Reproduziert vor dieser Aenderung: ein Tool, das haengt, hielt den
                        # Reader an — das darauffolgende Audio-Delta erreichte den Satelliten
                        # nicht, und ebensowenig speech_started/stopped, Transkripte oder der
                        # Silent-Stop. Uebergeben statt awaiten.
                        # M2: der Turn wandert MIT. Der Worker läuft neben dem Reader,
                        # und ohne diese Angabe könnte er ein Mandat aus einem anderen
                        # Turn einlösen.
                        self._tool_queue.put_nowait((calls, self._turn.get("turn_id", "")))
                    elif owns_turn:
                        self._mark("response_done")
                        log.info("core.response_done", session_id=self.session_id,
                                 turn_id=self._turn.get("turn_id"))
                        self._emit_turn()
                    else:
                        log.info("voice.dead_response_done",
                                 session_id=self.session_id)
                elif et == "error":
                    # Code und Feld EIGENS, nicht nur im abgeschnittenen
                    # Freitext: „nicht jeder Sitzungsfehler beweist einen
                    # falschen Sprachparameter". Wer den Grund aus einem bei
                    # 160 Zeichen gekappten Wortlaut raten muss, raet.
                    fehler = ev.get("error", {})
                    fehler = fehler if isinstance(fehler, dict) else {}
                    log.error("core.openai_error",
                              code=str(fehler.get("code", ""))[:60],
                              kind=str(fehler.get("type", ""))[:60],
                              param=str(fehler.get("param", ""))[:60],
                              detail=str(ev.get("error", {}))[:160])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("core.reader_error", kind=type(exc).__name__)
        # Hier endet die Schleife, ohne dass jemand sie abgebrochen hat: die
        # Anbieterverbindung ist weg. Frueher endete an dieser Stelle einfach
        # die Task, und niemand erfuhr davon — die Sitzung galt weiter als
        # aktiv, bis das naechste Audioframe in eine geschlossene Verbindung
        # lief und die Nachrichtenschleife des Satelliten mitriss. Fuer den
        # Menschen war das ein Geraet, das mitten im Satz verstummt.
        self._on_provider_lost(audio_generation)

    def _on_provider_lost(self, audio_generation: int | None = None) -> None:
        if audio_generation is not None and audio_generation != self.open_attempts:
            return
        if self._closing or self._stopping or not self.active:
            return
        # Die Generation freigeben, bevor der Wiederaufbau `open()` ruft — der
        # weigert sich zu Recht, solange ein Reader eingetragen ist.
        finished = self.reader
        self.reader = None
        if finished is not None:
            self._draining = [t for t in self._draining if t is not finished]
        self.active = False
        # Beides zuruecksetzen, und das ist kein Aufraeumen, sondern ein Fix:
        # `_timeout_loop` tastet die Sitzung bei jedem Tick an, solange
        # `speaking` oder `responding` gilt. Reisst die Verbindung mitten in
        # einer Antwort ab, blieb `responding` fuer immer wahr — die Sitzung
        # lief dann nie in den Inaktivitaets-Timeout, `_busy` blieb gesetzt,
        # und KEIN Satellit konnte sich mehr verbinden. Nur ein Neustart des
        # Core haette das geloest.
        self.responding = False
        self.speaking = False
        self._observe_audio("lost")
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return  # Keep the measured loss; only suppress a second reconnect task.
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Begrenzt neu aufbauen — und dabei nichts doppeln.

        Das Gespraech gehoert dem Core, nicht der Verbindung: `open()` haengt
        den Verlauf aus dem Gespraechsspeicher wieder an, und der ist die
        Quelle. Eine Aeusserung kann dadurch nicht doppelt entstehen, und
        bereits gehoertes Assistentenaudio kann nicht wiederkommen — die neue
        Anbietersitzung erzeugt frisch, und die Kennungen der alten sind mit
        der alten Verbindung gestorben.

        Was in der Zwischenzeit gesprochen wird, laeuft in den Vorlauf und geht
        beim Bereitwerden hinaus. Genau dafuer ist er da.
        """
        audio_generation = self.open_attempts
        # Tool call IDs belong to the old provider. Stop that worker before a
        # new socket can receive its result; keep it owned until actually done.
        worker = self._tool_worker
        if worker is not None:
            self._tool_worker = None
            self._draining.append(worker)
            worker.cancel()
        await self._drain()
        while True:
            try:
                self._tool_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._tool_queue.task_done()
        old = self.oa
        self.oa = None
        if old is not None:
            try:
                await self._close_provider(old)
            except asyncio.CancelledError:
                self._handoff_prior_unconfirmed = True
                self._observe_audio("close_unknown", audio_generation)
                raise
            except Exception:  # noqa: BLE001
                self._handoff_prior_unconfirmed = True
                self._observe_audio("close_unknown", audio_generation)
            else:
                if (guard := getattr(self, "acceptance_guard", None)) is not None:
                    guard.provider_closed(old)
                confirmed = self._provider_close_confirmed(old)
                if not confirmed:
                    self._handoff_prior_unconfirmed = True
                self._observe_audio("close_confirmed" if confirmed
                    else "close_unknown", audio_generation)
        for attempt in range(1, RECONNECT_ATTEMPTS + 1):
            if self._closing:
                return
            await asyncio.sleep(RECONNECT_BACKOFF * attempt)
            log.info("voice.session_reconnecting", session_id=self.session_id,
                     attempt=attempt)
            try:
                await self.open(announce=False)
            except Exception as exc:  # noqa: BLE001
                log.error("voice.reconnect_failed", session_id=self.session_id,
                          attempt=attempt, kind=type(exc).__name__)
                continue
            if not self.active:
                # A replacement that already lost its reader is not recovery.
                # End this conversation through the existing cleanup path;
                # do not leave its new socket/workers behind or report ready.
                await self.close(reason="provider_lost")
                return
            log.info("voice.session_reconnected", session_id=self.session_id,
                     attempt=attempt)
            return
        # Aufgegeben. Sagen kann SOLVIO das gerade nicht — jede Stimme kommt
        # vom Anbieter, und genau der ist weg. Was bleibt, ist ein sauberes
        # Ende statt eines stummen Geraets: der Satellit erfaehrt es, und das
        # Gespraech bleibt beim Core. Beim naechsten „Hey Solvio" geht es
        # dort weiter, wo es aufgehoert hat.
        log.warning("voice.reconnect_gave_up", session_id=self.session_id,
                    attempts=RECONNECT_ATTEMPTS)
        await self.close(reason="provider_lost")

    async def _tool_loop(self) -> None:
        """Fuehrt Tool-Aufrufe ausserhalb des Reader-Pfads aus, einer nach dem anderen.

        Genau EIN Worker: dadurch bleibt die Reihenfolge zweier aufeinanderfolgender
        Tool-Runden erhalten, und beim Schliessen ist genau eine Task abzuraeumen. Kein
        Job-Framework, kein Scheduler — nur diese Warteschlange.

        Ein Fehler in einem Tool beendet weder den Worker noch den Reader.
        """
        try:
            while True:
                calls, turn_id = await self._tool_queue.get()
                try:
                    await self._handle_tool_calls(calls, turn_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.error("core.tool_loop_error", session_id=self.session_id,
                              **describe_exception(exc))
                finally:
                    self._tool_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _handle_tool_calls(self, calls: list, turn_id: str = "") -> None:
        # Zuerst: hat der Mensch um Ruhe gebeten? Dann ist alles andere
        # gegenstandslos, und jede weitere Zeile kostet Zeit, in der geredet
        # wird. Der Aufruf erreicht den Dispatcher gar nicht — er ist eine
        # Anweisung an den Core, keine Faehigkeit.
        if not await self._tool_call_current(turn_id):
            return
        if any((c.get("name") or "") == "end_conversation" for c in calls):
            # Der Core hat das letzte Wort, nicht das Modell.
            #
            # Gemessen: „Fernseher aus" beendete zweimal das Gespraech, weil das
            # Modell den Befehl als Abschied deutete. Die Kontrollspur laesst
            # ihn korrekt durch — der Werkzeugweg hatte keine Bremse.
            if looks_like_a_device_command(self.turn_user_text or ""):
                log.info("core.END_CONVERSATION_REFUSED",
                         session_id=self.session_id, turn_id=turn_id,
                         reason="device_command")
            else:
                log.info("core.END_CONVERSATION_REQUESTED",
                         session_id=self.session_id, turn_id=turn_id)
                await self._silent_stop()
                return
        self.responding = True
        self.touch()
        disp = self.server.dispatcher
        # M2: für welchen Turn wird hier gearbeitet. Ein Mandat aus einem anderen Turn
        # kann damit nicht eingelöst werden.
        gate = getattr(disp, "memory_gate", None)
        if gate is not None:
            gate.expect_turn(self.session_id, turn_id)
        # Der vertrauenswuerdige Aufrufkontext fuer Faehigkeiten. Principal aus der
        # Authentifizierung, Herkunft aus der Art der Eingabe, Nutzertext aus dem
        # Transkript. Nichts davon kann das Modell setzen.
        # AUF DAS TRANSKRIPT WARTEN, BEVOR DIE POLITIK ENTSCHEIDET.
        #
        # Live gefunden: „Trag mir morgen um 15 Uhr einen Termin ein" wurde als
        # `turn_not_a_command` eingestuft — nicht weil der Satz keiner waere,
        # sondern weil der Werkzeugaufruf des Modells VOR dem fertigen
        # Transkript ankam. Gemessen wurde gegen leeren Text.
        #
        # Unter der alten Schwelle fiel das nicht auf: leerer Text hiess
        # „modellgewaehlt" hiess „Freigabe", und die war ohnehin faellig. Seit
        # die Herkunft ueber Reibung entscheidet, waere daraus ein Telefon
        # geworden, das mal fragt und mal nicht — und Unvorhersehbarkeit ist
        # schlimmer als Strenge, weil sie sich nicht lernen laesst.
        #
        # Das Warten laeuft in der Werkzeugschleife, nicht im Leseweg: es
        # blockiert nichts, was Audio bewegt. Bleibt das Transkript aus, gilt
        # weiterhin „kein belegter Auftrag" — also genau das alte Verhalten.
        await self._await_turn_text()
        invocation = getattr(disp, "capability_gate", None)
        if invocation is not None:
            turn_origin = origin_for_session(
                self.channel,
                interactive_proof=bool(getattr(self, "interactive_proof", False)),
                browser_task_session=getattr(self, "browser_task_session", None),
                session_id=self.session_id)
            # A browser that ended before this turn is not an authenticated
            # room microphone. Losing its proof must not create a phone request.
            turn_principal = self.satellite_id
            if self.channel == "voice_browser" and turn_origin is not OriginClass.TRUSTED_DASHBOARD:
                turn_principal = ""
            invocation.begin_turn(
                session_id=self.session_id, turn_id=turn_id,
                principal=turn_principal,
                trust=voice_trust(bool(self.satellite_id)),
                user_text=self.turn_user_text,
                # DIE HERKUNFT. Aus dem Kanal (Transportwahrheit, zwei
                # Schreibstellen) und dem Sitzungsbeweis — nie aus dem Modell.
                # Ohne App-Attest-Sitzungs-Assertion faellt das Telefon auf die
                # Raum-Zeile: voll benutzbar, mit dem Face-ID-Verhalten von V1.
                origin=turn_origin,
                # Ein VERWEIS, keine Autoritaet. Er entscheidet nichts an der
                # Matrix; er bindet Fortsetzung und Gleicharbeit an dieses eine
                # Gespraech.
                conversation_id=self.conversation_id or "",
                app_task_session=getattr(self, "app_task_session", None),
                browser_task_session=getattr(self, "browser_task_session", None))
        freigabe_gesehen = False
        for c in calls:
            if not await self._tool_call_current(turn_id):
                return
            name = c.get("name", "")
            call_id = c.get("call_id", "")
            args = disp.parse_args(c.get("arguments")) if disp else {}
            log.info("core.TOOL_CALL", name=name, session_id=self.session_id,
                     turn_id=turn_id or self._turn.get("turn_id"), call_id=call_id)
            # Ein Endpunkt darf erfahren, dass eine Faehigkeit begonnen hat —
            # den NAMEN, mehr nicht. Was daraus wird (etwa „das dauert, ich
            # melde mich"), entscheidet der Endpunkt; der Core faellt hier keine
            # Anzeigeentscheidung. Voreingestellt gibt es den Haken nicht, und
            # damit aendert sich fuer den Satelliten kein einziges Byte.
            notify = getattr(self, "on_capability_started", None)
            if notify is not None:
                try:
                    await notify(name)
                except Exception as exc:  # noqa: BLE001 - Anzeige darf nie ausfuehren stoeren
                    log.info("core.notify_failed", name=name,
                             kind=type(exc).__name__)
            t_tool = time.monotonic()
            # Jeder Aufruf wird EINZELN abgesichert. Vorher riss eine Ausnahme die ganze
            # Batch ab: die restlichen Aufrufe wurden nie ausgefuehrt, ihre
            # function_call_output nie gesendet, und response.create ebenso wenig — der
            # Provider wartete auf Ausgaben, die nie kamen, bis der Inaktivitaets-Timeout
            # griff. Reproduziert: drei Aufrufe, der zweite wirft -> ein einziges Output,
            # kein response.create, Turn haengt. Das Modell erfaehrt jetzt, dass das Tool
            # fehlschlug, statt auf eine Antwort zu warten, die niemand schickt.
            try:
                if not await self._tool_call_current(turn_id):
                    return
                result = (await disp.dispatch(name, args)) if disp else \
                    {"success": False, "error": "no_dispatcher"}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                info = describe_exception(exc)
                result = {"success": False, "error": "tool_failed", "kind": info["kind"]}
                log.error("core.tool_failed", name=name, session_id=self.session_id,
                          call_id=call_id, **info)
            if str((result or {}).get("error", "")).startswith("approval_required"):
                freigabe_gesehen = True
            # `ok` allein hat einen 25-Sekunden-„laeuft noch" wie ein Ergebnis
            # aussehen lassen — im Log stand `ok=True tool_ms=25005`, und
            # genau so wurde es auch erzaehlt. `settled` trennt „der Aufruf
            # gelang" von „die Sache ist erledigt".
            log.info("core.TOOL_DONE", name=name, session_id=self.session_id,
                     call_id=call_id, tool_ms=int((time.monotonic() - t_tool) * 1000),
                     ok=bool(result.get("success")), settled=_settled(result))
            self._watch_deep_result(name, result)
            await self._return_tool_result(call_id, result)
        # Und erst hier steht fest, WAS die Werkzeuge geantwortet haben — ob
        # eine Freigabe im Spiel war und ob ein Auftrag entstand. Deshalb misst
        # der Schatten Werkzeug-Turns von hier aus und nicht aus dem Reader.
        namen = tuple(str(c.get("name", "")) for c in calls if c.get("name"))
        art = "auftrag" if any(n.startswith("agent_task") for n in namen) else "faehigkeit"
        self._offer_shadow(observed_kind=art, tools=namen,
                           approval=freigabe_gesehen, turn_id=turn_id)
        await self._resume_after_tools()

    async def _silent_stop(self) -> None:
        if self._stopping or self._closing:
            return
        self._stopping = True
        # Das Tor zu, BEVOR irgendetwas gesendet wird: ab hier wird kein Frame
        # der alten Antwort mehr weitergereicht, ganz gleich, was noch
        # eintrudelt. Ohne diese Zeile konnte ein Delta, das zwischen Abbruch
        # und Schliessen ankam, noch zum Endpunkt gelangen und dort sprechen.
        self._audio_open = False
        if self._response_id:
            self._dead_responses.append(self._response_id)
        # Ob schon Ton unterwegs war, entscheidet, ob der Mensch etwas GEHOERT
        # hat. Der Anbieter beginnt zu sprechen, sobald die Sprecherkennung das
        # Satzende meldet — also moeglicherweise BEVOR das Transkript existiert,
        # an dem der Stopp haengt. Ohne diese Zahl bleibt „er redet trotzdem
        # noch kurz weiter" eine Vermutung.
        log.info("core.SILENT_STOP", audio_was_open=self._audio_open,
                 heard_ms=self._ms(self._turn.get("first_audio_sent"),
                                   time.monotonic()) if self._turn else None)
        try:
            await self.oa.send(json.dumps({"type": "response.cancel"}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.ws.send(json.dumps({"type": "flush"}))
        except Exception:  # noqa: BLE001
            pass
        await self.close(reason="silent_stop")

    async def _timeout_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.speaking or self.responding:
                    self.touch()
                    continue
                if time.monotonic() - self.last_activity >= self.server.idle_timeout:
                    log.info("core.SESSION_TIMEOUT")
                    await self.close(reason="timeout")
                    return
        except asyncio.CancelledError:
            pass

    #: M0/3: warum eine Sitzung endete, als KATEGORIE. Das Audit sah viele Sitzungen am
    #: 30-Sekunden-Timeout enden, konnte das aber nicht von einem normalen Ende unterscheiden.
    #: Die Politik selbst bleibt unveraendert — hier entsteht nur die Evidenz dafuer.
    _CLOSE_REASONS = {
        "silent_stop": "silent_stop",
        "timeout": "inactivity_timeout",
        "open_failed": "provider_error",
        "disconnect": "satellite_disconnect",
        "pi": "normal_close",
        # Derselbe ordentliche Abschied, seit die Schleife von Satellit und
        # iPhone dieselbe ist.
        "endpoint": "normal_close",
        "provider_lost": "provider_error",
    }

    async def close(self, reason: str) -> None:
        audio_generation = self.open_attempts
        # Zuerst und bedingungslos: der gehaltene Satzanfang endet hier.
        #
        # Ganz oben, weil weiter unten zwei Abkuerzungen stehen. Die zweite —
        # „nichts offen, nichts zu tun" — trifft ausgerechnet den Fall, auf den
        # es ankommt: der Satellit war verbunden, hat gesprochen, und das
        # Oeffnen der Anbietersitzung ist gescheitert. Dann gibt es keinen
        # Reader und kein `oa`, und rohes Raumaudio haette die Sitzung
        # ueberlebt. Ein Test hat das gefunden, nicht das Nachdenken.
        self.preroll.clear()
        # _closing heisst "laeuft gerade", nicht "war schon mal". Frueher blieb es fuer
        # immer gesetzt, wodurch die Sitzung nach dem ersten Schliessen dauerhaft gesperrt
        # war: ein spaeteres session_start auf derselben Verbindung konnte nichts mehr
        # oeffnen. Reproduziert als Smoke A.
        if self._closing:
            return
        # Nichts offen: nichts zu tun. _handle_pi ruft close() im finally IMMER auf, auch
        # nach einem regulaeren session_end — ohne diese Pruefung ginge ein zweites
        # session_end an den Pi und das Log zaehlte die Sitzung doppelt.
        if (self.reader is None and self.oa is None and not self.active
                and not any(t is not None and not t.done()
                            for t in (self.timer, self._tool_worker, self._persist_worker,
                                      self._open_task, self._reconnect_task))):
            # Mit einer Ausnahme, und die ist der haeufigste schlechte Fall:
            # der Satellit hat `session_start` geschickt, das Oeffnen ist
            # gescheitert (kein Reader, kein `oa`, nicht aktiv) — und diese
            # Abkuerzung schickte ihm nie ein `session_end`. Er blieb in ACTIVE
            # stehen und wartete auf ein `session_ready`, das nie kam.
            if self.open_attempts and not self._told_satellite:
                self._told_satellite = True
                try:
                    await self.ws.send(json.dumps({"type": "session_end",
                                                   "reason": reason}))
                except Exception:  # noqa: BLE001
                    pass
                log.info("core.session_end_after_failed_open",
                         session_id=self.session_id, reason=reason)
            self._observe_audio("ended", audio_generation)
            return
        self._closing = True
        # Ownership starts before persistence can yield, not only when the
        # later session_end frame reaches the endpoint transport.
        self._closing_task = asyncio.current_task()
        self._observe_audio("closing", audio_generation)
        self.active = False
        classified = self._CLOSE_REASONS.get(reason, "other")
        log.info("core.session_closing", session_id=self.session_id, close_reason=classified,
                 raw_reason=reason, turns=self.turn_no,
                 conversation_id=self.conversation_id,
                 conversation_mode=self.conversation_mode,
                 dropped_audio_frames=self.dropped_audio_frames,
                 # Kam ueberhaupt Ton an? `_frames_in` wurde gezaehlt und nie
                 # ausgegeben, und `first_frame_after_start_ms` stand nur in
                 # `session_ready` — also nur fuer das Oeffnungsfenster. Damit
                 # sah „das Mikrofon lieferte nichts" genauso aus wie „es kam
                 # an und war unverstaendlich". Beim iPhone-Endpunkt hat genau
                 # diese Verwechslung drei Anlaeufe gekostet.
                 frames_in=self._frames_in,
                 guarded_frames=self._guarded_frames,
                 first_frame_after_start_ms=self._ms(self.t_session_start,
                                                     self.t_first_frame),
                 session_age_ms=self._ms(self.t_connected, time.monotonic()))
        self._emit_turn()          # ein angefangener Turn geht nicht verloren
        await self._drain_persistence()
        store = self.server.conversations
        if store is not None and self.conversation_id is not None:
            try:
                store.end_session(self.session_id, classified)
            except ConversationStoreError as exc:
                log.error("core.conversation_store_error", session_id=self.session_id,
                          conversation_id=self.conversation_id, stage="end_session",
                          **describe_exception(exc))
        try:
            _disp = self.server.dispatcher
            if _disp is not None and getattr(_disp, "approvals", None) is not None:
                _disp.approvals.clear()
            if _disp is not None and getattr(_disp, "memory_gate", None) is not None:
                _disp.memory_gate.clear()
            # Der Dispatcher ist prozessweit und wird von jeder Sitzung geteilt. Bliebe
            # der Aufrufkontext stehen, koennte die naechste Sitzung unter dem Principal
            # der vorigen handeln.
            if _disp is not None and getattr(_disp, "capability_gate", None) is not None:
                _disp.capability_gate.clear()
        except Exception:  # noqa: BLE001
            pass
        # session_end ZUERST an den Pi (bevor Tasks gecancelt werden: self.timer
        # kann die aktuell laufende Task sein, deren Cancel close() sonst abbricht
        # und der Pi bliebe in ACTIVE haengen).
        try:
            self._told_satellite = True
            await self.ws.send(json.dumps({"type": "session_end", "reason": reason}))
        except Exception:  # noqa: BLE001
            pass
        provider_closed = False
        if self.oa is not None:
            try:
                await self._close_provider(self.oa)
            except asyncio.CancelledError:
                self._observe_audio("close_unknown", audio_generation)
                raise
            except Exception:  # noqa: BLE001
                self._observe_audio("close_unknown", audio_generation)
            else:
                if (guard := getattr(self, "acceptance_guard", None)) is not None:
                    guard.provider_closed(self.oa)
                provider_closed = self._provider_close_confirmed(self.oa)
                self._observe_audio("close_confirmed" if provider_closed
                    else "close_unknown", audio_generation)
            self.oa = None
        await self._confirm_conversation_flush(provider_closed=provider_closed)
        # Noch eingereihte Tool-Runden gehoeren dieser Generation. Sie zu behalten waere
        # falsch: der Worker der naechsten Sitzung wuerde Ergebnisse zu call_ids schicken,
        # die der neue Provider nie vergeben hat. Verwerfen — aber gezaehlt, nicht still.
        dropped = 0
        while True:
            try:
                self._tool_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._tool_queue.task_done()
            dropped += 1
        # Die Generation freigeben, BEVOR gecancelt wird. close() laeuft womoeglich INNERHALB
        # von self.reader (Silent-Stop) oder self.timer (Timeout); ein Cancel der eigenen
        # Task beendet alles Folgende. Was noch passieren muss, passiert deshalb vorher.
        # M0/3: der Tool-Worker wird mit abgeraeumt — sonst liefe ein haengendes Tool nach
        # dem Sitzungsende weiter und schriebe sein Ergebnis in eine geschlossene Verbindung.
        # Die Oeffnung gehoert dazu, seit sie nebenher laeuft. Ohne sie wuerde
        # eine halb geoeffnete Anbietersitzung nach dem Schliessen fertig
        # werden und in eine tote Sitzung hineinschreiben — `active` setzen,
        # `session_ready` senden, den Vorlauf abgeben. Ein bestehender Test
        # (`t_f_close_cancels_every_task_it_created`) hat genau das gefunden.
        tasks = list(dict.fromkeys(t for t in (self.reader, self.timer, self._tool_worker,
                             self._persist_worker, self._open_task,
                             self._reconnect_task, *self._draining)
                 if t is not None and not t.done()))
        self._draining = tasks
        self.reader = self.timer = self._tool_worker = self._persist_worker = None
        self._open_task = None
        self._reconnect_task = None
        self.active = False
        self._closing = False
        self._closing_task = None
        self._observe_audio("ended", audio_generation)
        log.info("core.SESSION_CLOSED", reason=reason, dropped_tool_rounds=dropped)
        current = asyncio.current_task()
        for t in tasks:
            if t is not current:
                t.cancel()
        if current in tasks:      # zuletzt: danach laeuft hier nichts mehr
            current.cancel()


def create_session(server, ws):
    """Select only the explicitly configured protocol; never retry on another model."""
    if server.model == "gpt-live-1":
        from solvio.realtime.live_session import LiveSession
        return LiveSession(server, ws)
    return Session(server, ws)


async def pump_endpoint(sess: "Session", frames: Any) -> None:
    """Die Nachrichtenschleife eines Sprachendpunkts — fuer jeden Transport dieselbe.

    Der Satellit und das iPhone sprechen dasselbe Protokoll, und genau deshalb
    steht es hier EINMAL. Waere es zweimal da, wuerde irgendwann eine der beiden
    Fassungen um eine Nachricht erweitert und die andere nicht — und der
    Unterschied faellt erst im Betrieb auf.

    `frames` ist alles, worueber sich `async for` laufen laesst und dabei
    `bytes` (Audio) oder `str` (JSON) liefert. Mehr braucht diese Schleife von
    einem Transport nicht; die Authentifizierung ist vorher passiert, und wer
    hier ankommt, ist bereits bewiesen.
    """
    async for message in frames:
        if isinstance(message, (bytes, bytearray)):
            await sess.feed_audio(bytes(message))
            continue
        msg = json.loads(message)
        t = msg.get("type")
        if t == "session_start":
            # NEBENHER, nicht awaitet. Frueher stand hier ein
            # `await sess.open()` — und weil diese Schleife
            # waehrenddessen keine Nachricht mehr entgegennahm,
            # stand der Eingang fuer die gemessenen 530 bis 1675 ms
            # still. websockets bremst dann den Absender aus
            # (max_queue=16, rund 320 ms Audio), und was der
            # Satellit in seiner eigenen Warteschlange nicht mehr
            # unterbringt, entscheidet er allein — der Core hat es
            # verursacht und konnte es nicht einmal sehen.
            #
            # Als Task laeuft die Oeffnung weiter, waehrend die
            # Schleife jedes Frame entgegennimmt und in den Vorlauf
            # legt. Erst dadurch ist der Puffer ueberhaupt an der
            # Reihe.
            sess.begin_open()
        elif t == "session_end":
            # KEIN Abbruch der Schleife. Die Verbindung bleibt stehen, und ein
            # zweites `session_start` darf eine neue Sitzung oeffnen — genau das
            # pruefte `t_i_t6_the_protocol_path_opens_a_second_session`, und
            # genau das ging beim Zusammenlegen der Schleife kurz verloren.
            await sess.close(reason="endpoint")
        elif t == "hello":
            log.info("core.hello", info=str(msg.get("info", ""))[:80])
        elif t == "barge_in":
            # Das Geraet ist von sich aus still geworden, weil es den Menschen
            # gehoert hat — und sagt, wie weit es gekommen war. Es entscheidet
            # damit NICHT, dass die Runde vorbei ist; das tut der Core hier,
            # mit denselben Schritten wie bei der Erkennung des Anbieters.
            #
            # Der Grund fuer diesen Weg ist Zeit: bis die Erkennung des
            # Anbieters greift, vergehen gemessen 162 bis 752 ms. So lange
            # redet SOLVIO weiter, obwohl der Mensch schon spricht. Ein Mensch
            # verstummt in einem Bruchteil davon.
            if sess.responding:
                await sess.note_barge_in(msg.get("played_ms"))
        elif t == "heard":
            # KEIN Abbruch. Der Core hat bereits unterbrochen und `flush`
            # geschickt; das Geraet sagt nur noch, wie viel davon wirklich zu
            # hoeren war. Frueher trug diese Auskunft denselben Namen wie eine
            # Unterbrechung, und der Core brach daraufhin ein zweites Mal ab —
            # zwei verschiedene Dinge unter einem Namen sind zwei Fehler, die
            # sich gegenseitig verdecken.
            await sess.note_heard(msg.get("played_ms"))
        elif t == "underrun":
            # Der Satellit hat die Wiedergabe leerlaufen sehen. Das
            # ist keine Stoerung, sondern eine Auskunft: dort wurde
            # eine Luecke hoerbar. Der aeltere Sprachpfad kannte den
            # Fall, dieser hier nicht — die Meldung fiel bisher in
            # den namenlosen Zweig und war unsichtbar.
            log.info("voice.playback_underrun", session_id=sess.session_id)
        elif t == "pong":
            pass


def _scrub_jail_credentials() -> int:
    """Raeumt JEDE Zugangsdatei aus dem Hermes-Kaefig. Unbedingt, bei jedem Start.

    Vor Provider Broker V1 stand in diesen Dateien der echte Anbieterschluessel
    des Cores. Eine halb gescheiterte Provisionierung — Broker unten, Deep
    deaktiviert, ein Fehler mitten in der Botschleife — liesse sonst eine davon
    mit dem alten Wert liegen, waehrend der Kaefig laeuft und sie lesen darf.

    Geloescht wird, nicht ueberschrieben: eine Datei, die es nicht gibt, kann
    keinen veralteten Wert halten. Deep und die Bots schreiben sie unmittelbar
    danach neu; fehlt sie, starten sie gar nicht. Fehler werden protokolliert
    und verschluckt — dieser Aufruf darf den Start nie verhindern.
    """
    jail = (os.environ.get("SOLVIO_DEEP_JAIL", "") or "").strip()
    if not jail:
        return 0
    home = os.path.join(jail, "home")
    targets = [os.path.join(home, ".env")]
    profiles = os.path.join(home, "profiles")
    try:
        for name in sorted(os.listdir(profiles)):
            targets.append(os.path.join(profiles, name, ".env"))
    except OSError:
        pass
    removed = 0
    for path in targets:
        try:
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        except OSError as exc:
            log.error("broker.scrub_failed", kind=type(exc).__name__)
    log.info("broker.jail_credentials_scrubbed", removed=removed,
             candidates=len(targets))
    return removed


def satellite_bind_hosts(host: str) -> list[str]:
    """Zerlegt die Bindeangabe des Satellitenports in eine Liste von Adressen.

    Warum es diese Funktion gibt, in einem gemessenen Satz: Am 2026-09-04 war
    der Satellitenport von einem Rechner im oeffentlichen Internet aus offen.
    Der Mac haelt seit STEP 21.2E einen WireGuard-Tunnel zum Knoten; der
    Vorgabewert `0.0.0.0` bindet auch dessen Overlay-Schnittstelle, und ein
    Verbindungsaufbau vom Knoten auf die Overlay-Adresse dieses Rechners kam
    durch. Der Port ist Klartext mit einem geteilten Geheimnis und ohne
    Sperrliste — `satellite_auth.py` sagt das ueber sich selbst.

    Der Fehler war nie eine Absicht, sondern eine Bindeadresse. Eine Liste
    erlaubt genau die Wege, die es geben soll — Rueckschleife und LAN —, ohne
    dass ein spaeter hinzukommendes Tunnel-Interface automatisch dazugehoert.
    Welche Adressen das konkret sind, sagt die Dienstdefinition, nicht diese
    Datei: eine nutzerspezifische Adresse gehoert nie in den Quelltext.

    `0.0.0.0` bleibt die Vorgabe und bleibt zulaessig: Diese Funktion aendert
    keine Voreinstellung, sie macht eine engere Angabe ueberhaupt erst
    ausdrueckbar. Was schuetzt, ist die Konfiguration der Produktion — nicht
    ein Standardwert, der auch fuer Testlaeufe und fremde Baeume gilt.
    """
    hosts = [part.strip() for part in (host or "").split(",")]
    hosts = [h for h in hosts if h]
    if not hosts:
        raise ValueError("keine Bindeadresse fuer den Satellitenport")
    return hosts


class CoreServer:
    # M1: Vorgabe auf Klassenebene. Ohne Gespraechsspeicher laeuft die Sprachschicht
    # vollstaendig weiter — sie ist dann nur zustandslos. Das darf kein AttributeError
    # sein, und keine Instanz darf ohne dieses Attribut existieren.
    conversations: "ConversationStore | None" = None

    def __init__(self, api_key: str, model: str, host: str, port: int,
                 voice: str = "marin", eagerness: str = "high",
                 phone_eagerness: str = "high", effort: str = "minimal",
                 idle_timeout: float = 30.0, dispatcher=None, credentials=None,
                 conversations: "ConversationStore | None" = None) -> None:
        self.dispatcher = dispatcher
        self._closing = False
        self.deep_service = None
        self.broker = None
        #: Die Beobachter laufender Recherchen. Sie haengen am PROZESS und
        #: nicht an einer Sitzung — eine Sprachsitzung schliesst nach
        #: `idle_timeout`, eine Recherche dauert Minuten. Fluechtig wie alles
        #: hier: stirbt der Core, storniert der Neustart ohnehin jede
        #: unfertige Aufgabe.
        self.deep_followup = DeepFollowUp(
            runtime=lambda: getattr(self.dispatcher, "deep_runtime", None),
            store=lambda: getattr(self.dispatcher, "proactive_store", None),
            timeout=DEEP_TASK_TIMEOUT)
        self.api_key = api_key.strip()
        self.model = model
        self.host = host
        self.port = port
        self.voice = voice
        self.eagerness = eagerness
        # Wie zurueckhaltend ein Geraet IN DER HAND sein soll.
        #
        # `eagerness` wurde fuer den Satelliten eingestellt: der steht fest in
        # einem bekannten Raum. Ein Telefon wandert, und neben ihm laeuft ein
        # Fernseher — dort haelt SOLVIO mit demselben Wert bei jedem Geraeusch
        # an. Es sind deshalb ZWEI Einstellungen und nicht eine, und jedes
        # kuenftige Sprachgeraet sagt hier, in welchem Raum es lebt.
        self.phone_eagerness = phone_eagerness
        self.effort = effort
        self.idle_timeout = idle_timeout
        self._busy = False
        self.credentials = credentials
        self.conversations = conversations
        self.room_history_owner = ""
        # Was der Satellit ueber sein eigenes Hoeren meldet. Der Core haelt nur
        # den letzten Bericht; die Zeitreihe bleibt im Journal des Geraets.
        self.satellite_health = SatelliteHealthRegistry()
        from .audio_observations import AudioObservations
        self.audio_observations = AudioObservations()

    async def _authenticate(self, ws: Any) -> str | None:
        """M0/2: prove this is our satellite BEFORE anything costly or privileged happens.

        Returns the satellite_id on success, None on refusal (the socket is closed here).
        Nothing above this line touches the provider, the dispatcher or Home Assistant — an
        unauthenticated client never reaches the session protocol at all.

        The challenge belongs to THIS connection and is consumed by the first attempt, so a
        captured response cannot be replayed: a new connection carries a different nonce, and
        the same connection accepts no second try.
        """
        peer = str(getattr(ws, "remote_address", "?"))
        if self.credentials is None:
            log.error("core.satellite_auth_unconfigured", peer=peer)
            await ws.close(code=1011, reason="satellite auth not configured")
            return None
        t_auth_begin = time.monotonic()
        challenge = SA.new_challenge()
        try:
            await ws.send(json.dumps({"type": "auth_challenge",
                                      "protocol_version": SA.PROTOCOL_VERSION,
                                      "server_nonce": challenge}))
            raw = await asyncio.wait_for(ws.recv(), timeout=SA.HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("core.satellite_auth_rejected", peer=peer, reason="handshake_timeout")
            await ws.close(code=4401, reason="auth timeout")
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("core.satellite_auth_rejected", peer=peer,
                        reason="handshake_transport", **describe_exception(exc))
            return None

        satellite_id = "<unknown>"
        try:
            if isinstance(raw, (bytes, bytearray)):
                raise ValueError("binary frame before authentication")
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "hello":
                raise ValueError("first frame is not a hello")
            satellite_id = str(msg.get("satellite_id", ""))[:64] or "<missing>"
            ok, reason = self.credentials.verify(
                satellite_id=str(msg.get("satellite_id", "")),
                server_nonce=challenge,
                client_nonce=str(msg.get("client_nonce", "")),
                protocol_version=msg.get("protocol_version"),
                auth=str(msg.get("auth", "")))
        except (ValueError, TypeError) as exc:
            ok, reason = False, "malformed_hello"
            log.warning("core.satellite_auth_rejected", peer=peer, satellite_id=satellite_id,
                        reason=reason, detail=sanitize_diagnostic(str(exc), limit=120))
            await ws.close(code=4400, reason="malformed hello")
            return None
        if not ok:
            # The reason is a CATEGORY. No secret, no HMAC, no credential material is logged.
            log.warning("core.satellite_auth_rejected", peer=peer,
                        satellite_id=satellite_id, reason=reason)
            await ws.close(code=4401, reason="unauthorized")
            return None
        log.info("core.satellite_authenticated", peer=peer, satellite_id=satellite_id,
                 auth_ms=int((time.monotonic() - t_auth_begin) * 1000))
        return satellite_id

    async def _route(self, ws: Any) -> None:
        """Eine Verbindung an die richtige Stelle geben.

        Der Sitzungsweg liegt auf `/`, so wie bisher — an ihm aendert sich
        nichts, und ein Satellit aelteren Standes findet ihn unveraendert vor.
        Der Gesundheitsbericht bekommt einen eigenen Pfad, damit er weder den
        `_busy`-Riegel anfasst noch eine Sitzung erzeugt: ein Bericht ist kein
        Gespraech, und waehrend eines Gespraechs muss er trotzdem ankommen.
        """
        request = getattr(ws, "request", None)
        path = getattr(request, "path", "/") or "/"
        if path.split("?", 1)[0].rstrip("/") == HEALTH_PATH.rstrip("/"):
            await self._handle_satellite_health(ws)
            return
        await self._handle_pi(ws)

    async def _handle_satellite_health(self, ws: Any) -> None:
        """Einen Gesundheitsbericht entgegennehmen — authentifiziert, dann Schluss.

        Dieselbe HMAC-Pruefung wie eine Sitzung: ein Geraet, das seine Kennung
        nicht beweisen kann, kann auch nichts ueber sie behaupten. Danach genau
        EINE Nachricht, eine Bestaetigung, und die Verbindung ist wieder zu.
        Es gibt hier keinen Zweig, der irgendetwas ausloest — ein Bericht ist
        Information und bleibt es.
        """
        satellite_id = await self._authenticate(ws)
        if satellite_id is None:
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=SA.HANDSHAKE_TIMEOUT)
            if isinstance(raw, (bytes, bytearray)):
                raise ValueError("binary frame on the health path")
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "satellite_health":
                raise ValueError(f"unexpected type {msg.get('type')!r}"
                                 if isinstance(msg, dict) else "not an object")
            report = parse_report(satellite_id, msg)
            self.satellite_health.record(report)
            # debug, nicht info: der Bericht kommt minuetlich und die
            # Gesundheitstafel liest ihn aus der Registry, nicht aus dem Log.
            # `logs/core.out.log` wird von launchd nicht rotiert — eine
            # minuetliche Zeile dort waere reines Wachstum ohne Leser.
            log.debug("satellite.health", satellite_id=satellite_id,
                     verdict=report.verdict, state=report.state,
                     chunks=report.hearing.get("chunks"),
                     rms_l_peak_db=report.hearing.get("rms_l_peak_db"),
                     repairs=report.repairs)
            await ws.send(json.dumps({"type": "health_ack"}))
        except Exception as exc:  # noqa: BLE001
            log.warning("satellite.health_rejected", satellite_id=satellite_id,
                        **describe_exception(exc))
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_pi(self, ws: Any) -> None:
        if getattr(self, "_closing", False):
            await ws.close(code=1001, reason="core_stopping")
            return
        t_connected = time.monotonic()
        satellite_id = await self._authenticate(ws)
        if satellite_id is None:
            return
        if getattr(self, "_closing", False):  # Authentication may have yielded during shutdown.
            await ws.close(code=1001, reason="core_stopping")
            return
        if self._busy:
            await ws.close(code=1013, reason="bereits ein Satellit aktiv")
            return
        self._busy = True
        sess = create_session(self, ws)
        sess.t_connected = t_connected
        sess.t_auth = time.monotonic()
        # Die HMAC-Pruefung hat diese Kennung bewiesen; bisher stand sie nur im Log.
        # Ab hier traegt die Sitzung sie, damit eine Faehigkeit weiss, WER fragt.
        sess.satellite_id = satellite_id
        sess.authenticated_room_device = satellite_id
        sess.observe_authenticated_device(satellite_id)
        log.info("core.PI_CONNECTED", peer=str(ws.remote_address), satellite_id=satellite_id,
                 session_id=sess.session_id,
                 satellite_connected_to_auth_ms=int((sess.t_auth - t_connected) * 1000))
        try:
            await pump_endpoint(sess, ws)
        except Exception as exc:  # noqa: BLE001
            log.error("core.pi_error", stage=sess.open_stage, **describe_exception(exc))
        finally:
            sess.audio_disconnected()
            await sess.close(reason="disconnect")
            self._busy = False
            log.info("core.PI_DISCONNECTED")

    async def _stop_child_runtimes(self) -> list[str]:
        """Beendet Browser und tiefen Executor. Fehler hier halten nichts auf."""
        failed = []
        # Zuerst die Beobachter: sie lesen aus dem Journal des tiefen Dienstes,
        # und der geht gleich zu.
        try:
            await self.deep_followup.close()
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
            failed.append("deep_followup")
            log.info("deep.followup_close_unclean", kind=type(exc).__name__)
        browser = getattr(self.dispatcher, "browser", None)
        if browser is not None:
            try:
                await browser.stop()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                failed.append("browser")
                log.error("browser.stop_failed", kind=type(exc).__name__)
        if self.deep_service is not None:
            try:
                await self.deep_service.stop()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                failed.append("deep")
                log.error("deep.stop_failed", kind=type(exc).__name__)
        # Der Broker zuletzt: solange ein Kaefigprozess noch atmet, soll er
        # eine ehrliche Absage bekommen und keinen toten Port.
        if self.broker is not None:
            try:
                await self.broker.stop()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                failed.append("broker")
                log.error("broker.stop_failed", kind=type(exc).__name__)
            self.broker = None
        return failed

    async def serve(self) -> None:
        # This outer lifetime also owns partially started bootstrap components.
        approver = control = scheduler = agent_runtime = supervisor = satellites = None
        try:
            # M0/2: say out loud which interface is being exposed and which satellites are
            # accepted. The listener used to bind 0.0.0.0 by default and log nothing about it,
            # so "reachable from the whole LAN" was an accident nobody could see in the log.
            log.info("core.satellite_listener", bind_host=self.host, port=self.port,
                     satellites=",".join(self.credentials.satellite_ids)
                     if self.credentials else "<none>")
            # Der produktive Freigabeweg laeuft IM Core-Prozess. Die bestaetigte
            # iPhone-Entscheidung lebt im Arbeitsspeicher des Approvers; ein zweiter
            # Prozess wuerde sie nie sehen, und eine erteilte Freigabe bliebe wirkungslos.
            # Vor allem anderen: die Gesundheitstafel muss den Satelliten finden
            # koennen, wenn sie gleich gebaut wird. Danach waere zu spaet und das
            # Ohr bliebe wieder unsichtbar — genau der Zustand, der diesen
            # Milestone ausgeloest hat.
            if self.dispatcher is not None:
                self.dispatcher.satellite_health = self.satellite_health

            approver = approver_from_env()
            if approver is not None:
                # Der Approver braucht den Dispatcher VOR dem Start: das
                # Kontrollzentrum haengt sich beim Bauen der Anwendung an und liest
                # von dort Aufgaben, Meldungen und Gesundheit. Danach gesetzt waere
                # es zu spaet, und die Ansichten blieben stumm.
                approver.dispatcher = self.dispatcher
                # Und dieser Server selbst: der Sprachweg des iPhones haengt sich an
                # dieselbe Anwendung und baut seine Sitzungen mit derselben Fabrik,
                # die der Satellit benutzt. Auch das muss VOR dem Start stehen —
                # danach ist die Anwendung schon gebaut.
                approver.voice_server = self
                try:
                    await approver.start()
                    if self.dispatcher is not None:
                        self.dispatcher.approver_runtime = approver
                        self.dispatcher.capabilities._mobile = approver.approvals
                        log.info("approvals.wired_into_capabilities")
                except Exception as exc:  # noqa: BLE001
                    # Fail-closed: ohne Freigabeweg bleiben schreibende Faehigkeiten
                    # bei `approval_required` stehen. Der Sprachpfad laeuft weiter.
                    log.error("approvals.start_failed", kind=type(exc).__name__,
                              detail=str(exc)[:200])
                    # Retain approver for shutdown after a partial start.

            from solvio.config import load_settings as _load_settings

            # UNBEDINGT und als ERSTES: jede Zugangsdatei im Kaefig ausraeumen.
            #
            # Das steht vor jeder Torpruefung und ausserhalb jedes `if`, weil sonst
            # der Satz „im Kaefig liegt nie ein wiederverwendbarer Zugang" nur
            # wahrscheinlich waere statt wahr. `serve()` verschluckt die Ausnahmen
            # der folgenden Bloecke; ein Fehlschlag mitten in der Botschleife liesse
            # sonst eine Profildatei mit dem ECHTEN Schluessel von gestern liegen,
            # waehrend das Gateway laeuft und sie lesen darf. Gefahrlos ist es
            # ohnehin: ohne `.env` starten Deep und Bots gar nicht, und die
            # Provisionierung schreibt die Datei unmittelbar danach neu.
            _scrub_jail_credentials()

            # ERST JETZT: offene Anrufe nachlesen — und NUR nachlesen.
            #
            # Dieser Block stand zuerst weiter oben, unmittelbar VOR dem
            # Kaefig-Ausraeumen. Das war falsch, und zwar aus zwei Gruenden, die
            # beide erst die Abnahme gemessen hat.
            #
            # Erstens steht direkt darueber der Satz „UNBEDINGT und als ERSTES".
            # Ein NETZAUFRUF davor macht aus „im Kaefig liegt nie ein
            # wiederverwendbarer Zugang" wieder ein „wahrscheinlich".
            #
            # Zweitens hatte der Aufruf keine Frist. `_recover_each` arbeitet die
            # offenen Zeilen NACHEINANDER ab, je Zeile ein Anbieteraufruf mit bis zu
            # 20 Sekunden — und ein Neustart nach Stromausfall ist genau die Lage,
            # in der offene Zeilen entstehen UND das Netz noch fehlt. Der Lauscher
            # startet danach; SOLVIO waere so lange taub gewesen. Der alte
            # try/except fing den Fehler, aber nicht die Zeit.
            #
            # Deshalb ein hartes Gesamtbudget. Laeuft es ab, bleiben die Zeilen
            # offen und der naechste Start versucht es erneut — das ist derselbe
            # Ausgang wie bei einem nicht erreichbaren Anbieter, und er ist
            # ehrlicher als ein Assistent, der nicht zuhoert.
            #
            # Es wird dabei niemand angerufen; `telephony_call` ist aus dieser
            # Herkunft ohnehin DENY.
            telephony = getattr(self.dispatcher, "telephony", None)
            if telephony is not None:
                try:
                    wieder = await asyncio.wait_for(
                        telephony.recover_open_calls(),
                        timeout=TELEPHONY_RECOVERY_BUDGET_SECS)
                    log.info("telephony.startup_recovery", recovered=len(wieder))
                except asyncio.TimeoutError:
                    log.warning("telephony.startup_recovery_timeout",
                                budget=TELEPHONY_RECOVERY_BUDGET_SECS)
                except Exception as exc:  # noqa: BLE001
                    # Ein Anbieter, der beim Hochfahren nicht antwortet, darf den
                    # Start nicht aufhalten. Die Zeilen bleiben offen und werden
                    # beim naechsten Start erneut versucht.
                    log.error("telephony.startup_recovery_failed",
                              kind=type(exc).__name__, detail=str(exc)[:200])

            # Der Provider Broker. Er gehoert dem Core, laeuft in DIESEM Prozess und
            # stirbt mit ihm — fail-closed. Er steht VOR Deep und den Bots, weil
            # beide ohne ihn nichts duerfen.
            broker = None
            try:
                from solvio.provider_broker import BrokerService
                settings = _load_settings()
                key = (getattr(settings, "openai_api_key", "") or "").strip()
                if key:
                    # Der Tresor gehoert HIERHER, nicht in den Adapter.
                    #
                    # Gemessen am 2026-09-03: `autopilot-writer-claude` hatte im
                    # Brokerbuch 22 Aufrufe und 0 Erfolge, alle mit
                    # `no_credential` — und zwar seit es ihn gibt. Der Grund war
                    # diese Zeile: ohne `vault` ist `AnthropicUpstream._vault`
                    # `None`, und dann ist die zweite Flaeche des Brokers
                    # unbenutzbar, egal was im Tresor liegt.
                    #
                    # Der Wert wird nicht gehalten, sondern je Anfrage geliehen
                    # und wandert in genau einen Kopfzeilenwert. Ein zweiter
                    # Broker im Treiberprozess waere die Alternative gewesen —
                    # und zwei Kappen sind keine Kappe.
                    from solvio.secret_vault.broker import SecretBroker
                    candidate = BrokerService(provider_key=key,
                                              vault=SecretBroker())
                    self.broker = candidate  # Own partial startup before its first await.
                    await candidate.start()
                    broker = candidate
                    self.broker = candidate
                    if self.dispatcher is not None:
                        # Dort sucht die Gesundheitssonde. Der Doktor bekommt
                        # bewusst KEIN Playbook dafuer — der Broker startet mit dem
                        # Core, sonst waeren die Token im Kaefig verwaist.
                        self.dispatcher.provider_broker = candidate
                    log.info("broker.wired", port=candidate.port)
                else:
                    log.error("broker.no_provider_key")
            except Exception as exc:  # noqa: BLE001
                # Ein belegter Port (EADDRINUSE eingeschlossen) nimmt die Stimme
                # NICHT mit. Deep und die Bots verweigern dann ueber ihr eigenes
                # Tor; der Core antwortet weiter. Ein Broker, der den Core
                # mitnaehme, waere die schlechtere Schuld.
                log.error("broker.start_failed", kind=type(exc).__name__,
                          detail=str(exc)[:200])
                broker = None

            # Der tiefe Executor laeuft ausdruecklich NICHT in diesem Prozess: er
            # bekommt ein eigenes Gefaengnis, eine eigene Umgebung und ein
            # Seatbelt-Profil. Der Core spricht mit ihm ueber HTTP und behaelt
            # Identitaet, Abbruch, Frist und Freigabe bei sich.
            deep = deep_from_env(_load_settings(), broker)
            if deep is not None:
                # Der Griff wird SOFORT hinterlegt — auch wenn der Start gleich
                # scheitert. Sonst findet der Doktor bei einem gescheiterten
                # ERSTstart keinen Dienst, den er neu starten koennte, und meldet
                # eine unmoegliche Reparatur, bis er aufgibt. Genau das ist
                # produktiv passiert.
                self.deep_service = deep
                if self.dispatcher is not None:
                    self.dispatcher.deep_service = deep
                try:
                    runtime = await deep.start()
                    if self.dispatcher is not None:
                        names = attach_deep_runtime(self.dispatcher, runtime)
                        log.info("deep.wired_into_capabilities",
                                 capabilities="+".join(names),
                                 toolsets="+".join(deep.toolsets))
                except Exception as exc:  # noqa: BLE001
                    # Ohne echte Isolation gibt es keine tiefe Ausfuehrung. Die
                    # Sprachschicht laeuft vollstaendig weiter, nur ohne Recherche.
                    log.error("deep.start_failed", kind=type(exc).__name__,
                              detail=str(exc)[:200])
                    try:
                        await deep.stop()
                    except Exception:  # noqa: BLE001
                        pass

            # Das Botteam. Es braucht dasselbe Gefaengnis wie der tiefe Executor,
            # aber ausdruecklich NICHT dessen Gateway: ein Bot ist ein eigener,
            # kurzlebiger Prozess im selben Seatbelt-Profil. Deshalb steht es hier
            # und nicht in `build_dispatcher` — die Profile werden angelegt und mit
            # SOLVIOs Haltung beschrieben, bevor die erste Frage kommt.
            if self.dispatcher is not None:
                try:
                    from solvio.bots.team import from_environment as bots_from_env
                    from solvio.tools.registry import attach_bot_team
                    team = bots_from_env(_load_settings(), broker)
                    if team is not None:
                        state = await team.provision()
                        names = attach_bot_team(self.dispatcher, team)
                        log.info("bots.wired_into_capabilities",
                                 capabilities="+".join(names),
                                 ready=sum(1 for e in state.values() if e.ok),
                                 total=len(state))
                except Exception as exc:  # noqa: BLE001
                    # Ohne Botteam laeuft SOLVIO vollstaendig weiter, nur ohne
                    # Fachauskunft. Kein Halbzustand: entweder die Profile stehen,
                    # oder die Faehigkeit ist gar nicht registriert.
                    log.error("bots.start_failed", kind=type(exc).__name__,
                              detail=str(exc)[:200])

            # Der oertliche Griff. Er entsteht erst hier, NACH dem Approver: was er
            # anbietet, soll nicht schon erreichbar sein, bevor der Freigabeweg steht.
            control = None
            if os.environ.get("SOLVIO_CONTROL_SOCKET", "1") not in ("0", "off", "no"):
                from solvio.realtime.control import DEFAULT_SOCKET, CoreControl
                control = CoreControl(self.dispatcher,
                                      socket_path=os.environ.get("SOLVIO_CONTROL_SOCKET_PATH",
                                                                 DEFAULT_SOCKET))
                try:
                    await control.start()
                    if self.model == "gpt-live-1":
                        from solvio.realtime.live_status_tools import attach as attach_live_status
                        attach_live_status(control)
                except Exception as exc:  # noqa: BLE001
                    # Der Griff ist Komfort, kein Betriebsmittel. Faellt er aus,
                    # laeuft der Sprachpfad unveraendert weiter.
                    log.error("core.control_start_failed", kind=type(exc).__name__,
                              detail=str(exc)[:200])
                    # Retain control for shutdown after a partial start.
            # Der Hintergrund. Zuletzt, weil er beides braucht: den Freigabeweg fuer
            # eine faellige Bestaetigung und den tiefen Ausfuehrenden fuer eine
            # geplante Recherche.
            #
            # SIGTERM drains these components. Durable recovery remains necessary
            # for SIGKILL, power loss and interrupted physical outcomes.
            scheduler = None
            if os.environ.get("SOLVIO_SCHEDULER", "1") not in ("0", "off", "no"):
                store = getattr(self.dispatcher, "proactive_store", None)
                if store is not None:
                    from solvio.proactive.scheduler import Scheduler
                    scheduler = Scheduler(self.dispatcher, store)
                    try:
                        await scheduler.start()
                        self.dispatcher.scheduler = scheduler
                        if getattr(self.dispatcher, "proactive", None) is not None:
                            self.dispatcher.proactive.scheduler = scheduler
                    except Exception as exc:  # noqa: BLE001
                        log.error("proactive.scheduler_start_failed",
                                  kind=type(exc).__name__, detail=str(exc)[:200])
                        # Retain scheduler for shutdown after a partial start.
            def _development_wiring():
                """Entwicklungsbuch UND Treiberfabrik — aus EINER Hand.

                Beide muessen dasselbe Buch benutzen: `commission()` schreibt den
                Auftrag hinein, der Treiber liest ihn daraus. Zwei Buecher waeren
                zwei Wahrheiten, und der Treiber faende den Auftrag nicht.

                Gebaut wird ueber `DEV.build_driver_factory` — dieselbe Funktion,
                die auch die Abnahme ruft. Frueher stand die Konfiguration nur in
                der Testanordnung, und der Core uebergab gar keine Fabrik: der
                automatische Start gab es damit ausschliesslich im Test.
                """
                try:
                    from solvio.agent_runtime import development as _DEV
                    from solvio.agent_runtime.workspace import WorkspaceManager
                    from solvio.autopilot.store import AutopilotLedger
                    buch = AutopilotLedger()
                    fabrik = _DEV.build_driver_factory(
                        ledger=buch, workspaces=WorkspaceManager(),
                        repo=os.path.dirname(os.path.dirname(os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))))),
                        python=sys.executable)
                    return buch, fabrik
                except Exception as exc:  # noqa: BLE001
                    log.warning("agent_runtime.development_unavailable",
                                kind=type(exc).__name__)
                    return None, None

            _dev_buch, _dev_fabrik = _development_wiring()

            # Die Agentenlaufzeit. Fail-soft und hinter einem Gate, wie der
            # Scheduler: schlaegt sie fehl, laeuft SOLVIO vollstaendig weiter — nur
            # ohne Agentenauftraege. Bis sie angehaengt ist, gibt es die fuenf
            # Faehigkeiten nicht; das ist zugleich die Rollback-Zusage.
            agent_runtime = None
            if os.environ.get("SOLVIO_AGENT_RUNTIME", "1") not in ("0", "off", "no"):
                try:
                    from solvio.agent_runtime.orchestrator import Orchestrator
                    from solvio.capabilities.approver_runtime import OWNER_PRINCIPAL
                    from solvio.agent_runtime.planner import planner_from_settings
                    from solvio.agent_runtime.native_costs import NativeSubscriptionCosts
                    from solvio.agent_runtime.native_credit_policy import NativeCreditPolicy
                    from solvio.agent_runtime.store import AgentRunLedger
                    from solvio.agent_runtime.workspace import WorkspaceManager
                    from solvio.tools.registry import attach_agent_runtime

                    agent_runtime = Orchestrator(
                        ledger=AgentRunLedger(),
                        require_task_authority=True,
                        cost_quote_adapter=NativeSubscriptionCosts(credit_policy=NativeCreditPolicy(
                            getattr(getattr(self.dispatcher, "approver_runtime", None), "approvals", None))),
                        router=getattr(self.dispatcher, "capabilities", None),
                        control_plane=getattr(
                            getattr(self.dispatcher, "approver_runtime", None),
                            "control_plane", None),
                        # Abo-Anmeldung wird vor jedem Dispatch geprueft.
                        # Die Sprach-API am Server ist kein Agenten-Rueckfall.
                        planner=planner_from_settings(_load_settings()),
                        proactive=getattr(self.dispatcher, "proactive_store", None),
                        conversations=self.conversations,
                        workspaces=WorkspaceManager(),
                        # Der Rechercheweg ist der bereits freigegebene Hermes-Seam,
                        # nicht der Router: `deep_*` bleibt auf der Agent-Sperrliste.
                        # Er wird oben angehaengt, steht hier also schon.
                        researcher=getattr(self.dispatcher, "deep_capabilities", None),
                        # **Derselbe** Gap Resolver, den der Werkzeugpfad benutzt —
                        # nicht ein zweiter. Bis hierher kannte ihn nur der
                        # Dispatcher, und was er fand, endete als Satz im
                        # Modellkontext. Ein Agentenlauf, der an einer fehlenden
                        # Faehigkeit haengenblieb, konnte nichts damit anfangen.
                        gap_resolver=getattr(self.dispatcher, "gap_resolver", None),
                        # Das Autopilot-Buch. Fehlt es, laeuft die Laufzeit
                        # vollstaendig weiter — nur ohne Entwicklungsauftraege.
                        development=_dev_buch,
                        # **Der Anschluss, der gefehlt hat.** Ohne ihn legt SOLVIO
                        # den Entwicklungsauftrag an und niemand faehrt ihn.
                        driver_factory=_dev_fabrik,
                        # Der kanonische Gedaechtnisabruf, nur lesend. Dieselbe
                        # `MemoryService`, die auch das Gespraech benutzt — kein
                        # zweiter Speicher und kein Schreibpfad.
                        knowledge=getattr(self.dispatcher, "memory", None),
                        personal_memory=getattr(self.dispatcher, "memory", None),
                        memory_owner_principal=OWNER_PRINCIPAL)
                    self.room_history_owner = OWNER_PRINCIPAL
                    # A separate empty adapter seed, no Core checkout mutation or
                    # provider invocation. The original task owns all later work.
                    from solvio.agent_runtime.extension_runtime import attach_document_runtime
                    try:
                        attach_document_runtime(agent_runtime)
                    except Exception as exc:
                        log.warning("agent_runtime.document_extension_unavailable",
                                    kind=type(exc).__name__)
                    adaptive = getattr(self.dispatcher, "adaptive_memory", None)
                    if adaptive is not None:
                        from solvio.memory.adaptive.observations import AdaptiveObservations
                        observations = AdaptiveObservations(adaptive, agent_runtime.ledger,
                            owner_principal=agent_runtime.memory_owner_principal,
                            quote_adapter=agent_runtime.cost_quote_adapter,
                            settlement_adapter=agent_runtime.cost_settlement_adapter)
                        agent_runtime.memory_observations = observations
                        self.dispatcher.memory_observations = observations
                    await agent_runtime.start()
                    attach_agent_runtime(self.dispatcher, agent_runtime)
                    if self.model == "gpt-live-1":
                        from solvio.agent_runtime.voice_delegate import LiveBackend
                        live_settings = _load_settings()
                        self.dispatcher.live_backend = LiveBackend(agent_runtime.ledger,
                            provider=live_settings.agent_runtime_subscription_provider,
                            model=live_settings.agent_runtime_subscription_model,
                            quote_adapter=agent_runtime.cost_quote_adapter,
                            settlement_adapter=agent_runtime.cost_settlement_adapter)
                except Exception as exc:  # noqa: BLE001
                    log.error("agent_runtime.start_failed",
                              kind=type(exc).__name__, detail=str(exc)[:200])
                    # Retain agent_runtime for shutdown after a partial start.

            # Der kognitive Router. ZULETZT, nach Deep, Bots und Agentenlaufzeit —
            # die Werkzeuge, deren Belichtung er umlegt, entstehen erst dort,
            # und wer frueher umlegt, legt nichts um. Fail-soft wie alles hier:
            # schlaegt er fehl, laeuft SOLVIO vollstaendig weiter, mit der
            # Oberflaeche von vorher.
            #
            # Der Gespraechsspeicher haengt am SERVER und war dem Dispatcher bisher
            # unbekannt. Der Router braucht ihn lesend — fuer den Ausschnitt und
            # fuer den Text eines Turns, zu dem eine Rueckfrage offen ist. Er
            # bekommt ihn hier, und ausdruecklich nur lesend: geschrieben wird in
            # den Gespraechsspeicher weiter allein aus dieser Datei.
            try:
                from solvio.cognition import normalise_mode
                from solvio.tools.registry import attach_cognition
                mode = normalise_mode(
                    getattr(_load_settings(), "cognitive_router_mode", "off"))
                self.dispatcher.conversations = self.conversations
                self.dispatcher.cognitive_router_mode = mode
                attach_cognition(self.dispatcher, mode=mode)
            except Exception as exc:  # noqa: BLE001
                log.error("cognition.attach_failed",
                          kind=type(exc).__name__, detail=str(exc)[:200])
                self.dispatcher.cognitive_router_mode = "off"

            if self.model == "gpt-live-1":
                from solvio.realtime.live_session import prepare_live_dispatcher
                prepare_live_dispatcher(self.dispatcher)

            # Der Arzt macht seine Runde. Er entsteht am Freigabeweg (dort haengt das
            # Gesundheitsbrett) und bekommt hier nur seine Schleife.
            supervisor = None
            doctor = getattr(self.dispatcher, "doctor", None)
            if doctor is not None and os.environ.get(
                    "SOLVIO_DOCTOR", "1") not in ("0", "off", "no"):
                from solvio.doctor.supervisor import Supervisor
                supervisor = Supervisor(
                    doctor, store=getattr(self.dispatcher, "proactive_store", None))
                try:
                    await supervisor.start()
                    self.dispatcher.supervisor = supervisor
                except Exception as exc:  # noqa: BLE001
                    log.error("doctor.supervisor_start_failed",
                              kind=type(exc).__name__)
                    # Retain supervisor for shutdown after a partial start.
            satellites = await ws_serve(self._route, satellite_bind_hosts(self.host),
                                        self.port, max_size=None)
            print("=== SOLVIO Core bereit (Session on demand) ===", flush=True)
            print(f"WebSocket ws://{self.host}:{self.port}", flush=True)
            print(f"OpenAI wird ERST bei 'session_start' geoeffnet. Inaktivitaets-Timeout {self.idle_timeout:.0f}s.", flush=True)
            await asyncio.Future()
        finally:
            cleanup = asyncio.create_task(self._shutdown_components(
                approver=approver, control=control, scheduler=scheduler,
                agent_runtime=agent_runtime, supervisor=supervisor, satellites=satellites))
            # Repeated caller cancellation cannot detach a child-process drain.
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                raise asyncio.CancelledError

    async def _shutdown_components(self, *, approver, control, scheduler,
                                   agent_runtime, supervisor, satellites) -> None:
        # Close admission before the first cleanup await; no model authority or
        # persisted task status changes here. Stop the doctor before its services.
        self._closing = True
        failed = []
        if satellites is not None:
            try:
                satellites.close()
            except Exception as exc:
                failed.append("satellites")
                log.error("core.shutdown_component_failed", component="satellites", kind=type(exc).__name__)
        if approver is not None:
            approver._stopping = True
        if control is not None:
            control._stopping = True

        async def close_one(name, callback):
            try:
                return await callback()
            except (Exception, asyncio.CancelledError) as exc:
                failed.append(name)
                log.error("core.shutdown_component_failed", component=name,
                          kind=type(exc).__name__)

        if supervisor is not None:
            await close_one("supervisor", supervisor.stop)
        if scheduler is not None:
            await close_one("scheduler", scheduler.stop)
        if control is not None:
            await close_one("control", control.stop)
        if approver is not None:
            await close_one("approver", approver.stop)
        if satellites is not None:
            await close_one("satellites", satellites.wait_closed)
        if agent_runtime is not None:
            await close_one("agent_runtime", agent_runtime.stop)
        adaptive = getattr(self.dispatcher, "adaptive_memory", None)
        if adaptive is not None:
            await close_one("adaptive_memory", adaptive.close)
        observations = getattr(self.dispatcher, "memory_observations", None)
        if observations is not None:
            try:
                observations.activities.hold_interrupted_observations()
            except Exception as exc:
                failed.append("observations")
                log.error("core.shutdown_component_failed", component="observations",
                          kind=type(exc).__name__)
        failed.extend(await close_one("child_runtimes", self._stop_child_runtimes) or [])
        memory = getattr(self.dispatcher, "memory", None)
        if memory is not None:
            await close_one("memory", memory.close)
        if self.conversations is not None:
            try:
                self.conversations.close()  # ConversationStore owns a sync connection.
            except Exception as exc:
                failed.append("conversations")
                log.error("core.shutdown_component_failed", component="conversations",
                          kind=type(exc).__name__)
        approval_store = getattr(approver, "_store", None)
        if approval_store is not None:
            await close_one("approval_store", approval_store.close)
        log.info("core.shutdown_finished", clean=not failed, failed_components="+".join(failed))


async def run_until_stopped(server: CoreServer) -> None:
    """CLI signal ownership; a second signal never interrupts ordered cleanup."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(server.serve())
    requested = False

    def request_stop():
        nonlocal requested
        if not requested:
            requested = True
            task.cancel()

    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    for signum in signals:
        loop.add_signal_handler(signum, request_stop)
    try:
        await task
    except asyncio.CancelledError:
        if not requested:
            raise
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous[signum])


if __name__ == "__main__":
    import argparse
    from solvio.config import load_settings
    from solvio.logging_setup import setup_logging

    p = argparse.ArgumentParser(description="SOLVIO Core Server (Session on demand)")
    # No user-specific address is baked into the source: the bind host is configuration.
    p.add_argument("--host", default=os.environ.get("SOLVIO_SATELLITE_BIND", "0.0.0.0"),
                   help="interface for the satellite listener (env: SOLVIO_SATELLITE_BIND)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--voice", default="marin")
    p.add_argument("--effort", default="minimal")
    p.add_argument("--eagerness", default="high",
                   help="Satellit: fest im Raum, dort eingestellt und freigegeben")
    p.add_argument("--phone-eagerness", default="high",
                   help="Geraet in der Hand: zurueckhaltender, weil es seinen Raum mithoert")
    p.add_argument("--idle-timeout", type=float, default=30.0)
    a = p.parse_args()

    s = load_settings()
    setup_logging(s.solvio_log_level)
    # Fail loudly rather than bind something unintended. Jede einzelne Adresse
    # der Liste wird geprueft — eine unbrauchbare darf nicht erst beim Lauschen
    # auffallen, wenn der Dienst schon als gestartet gilt.
    try:
        for _bind in satellite_bind_hosts(a.host):
            socket.getaddrinfo(_bind, a.port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        print(f"Ungueltige Bind-Adresse {a.host!r}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    try:
        credentials = SA.load_credentials()
    except SA.SatelliteAuthError as exc:
        print(f"Satellite-Authentifizierung nicht einsatzbereit: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not s.has_realtime:
        print("Realtime nicht konfiguriert (kein API-Key in .env).")
        raise SystemExit(2)
    from solvio.tools.registry import build_dispatcher
    dispatcher = build_dispatcher(s)
    from solvio.tools.registry import tools_health
    try:
        print(f"Tools-Health: {asyncio.run(tools_health(s, dispatcher))}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    # M1: Der Gespraechsspeicher ist Produktzustand, nicht Sicherheitszustand — deshalb
    # ein eigenes Verzeichnis (SOLVIO_STATE_DIR, Vorgabe ~/.solvio) und ausdruecklich NICHT
    # ~/.solvio-approvals. Faellt er aus, laeuft die Sprachschicht zustandslos weiter.
    conversations = None
    try:
        conversations = ConversationStore().open()
        print(f"Gespraechsspeicher: {conversations.path}", flush=True)
    except ConversationStoreError as exc:
        print(f"Gespraechsspeicher nicht verfuegbar, Sitzungen laufen zustandslos: {exc}",
              file=sys.stderr, flush=True)
    srv = CoreServer(api_key=s.openai_api_key, model=s.openai_realtime_model,
                     host=a.host, port=a.port, voice=a.voice, effort=a.effort,
                     eagerness=a.eagerness, phone_eagerness=a.phone_eagerness,
                     idle_timeout=a.idle_timeout,
                     dispatcher=dispatcher, credentials=credentials,
                     conversations=conversations)
    try:
        asyncio.run(run_until_stopped(srv))
    except KeyboardInterrupt:
        print("\nbeendet.")
