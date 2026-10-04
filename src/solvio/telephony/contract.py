"""Was ein Anruf ist, was aus ihm werden kann, und was SOLVIO darueber behaupten darf.

Dieses Modul ist anbieterneutral. Es kennt keinen Anbieter, keine URL und keinen
Schluessel — nur die geschlossenen Zustandsmengen und die Regeln, nach denen ein
Anbieterergebnis in sie uebersetzt wird. Ein Wechsel des Telefonieanbieters
laesst diese Datei unberuehrt; er tauscht den Adapter.

Die wichtigste Regel steht gleich am Anfang, weil sie der Grund fuer die ganze
Trennung ist:

    Ein gestarteter Anruf ist keine ausgerichtete Nachricht.

Der Anbieter kann melden, dass er gewaehlt hat. Das sagt nichts darueber, ob
jemand abgehoben, zugehoert oder verstanden hat. Deshalb gibt es ZWEI Mengen —
den Zustand der Verbindung und den Zustand der Nachricht — und keine Funktion,
die aus der ersten die zweite errät.

Warum eine geschlossene Menge und kein freier String: die Communication-Faehigkeit
hat es vorgemacht (`DELIVERY_TRUTHS` in `capabilities/communication.py`), und der
Grund steht dort — ein erfundener Wert waere eine Aussage ueber Zustellung, die
niemand geprueft hat. Hier gilt dasselbe, nur teurer: es geht um ein Telefonat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from solvio.contracts.trust import TrustLevel

# ---------------------------------------------------------------------------
# Der Zustand der VERBINDUNG
# ---------------------------------------------------------------------------
PREPARED = "PREPARED"
#: Freigegeben und gebunden, aber der Anbieter wurde noch nicht gerufen. In
#: diesem Zustand ist "es hat nicht stattgefunden" Wissen, nicht Hoffnung.

DIALING = "DIALING"
#: Der Anbieter hat den Auftrag angenommen. Ab hier ist die ehrliche Aussage
#: "es klingelt moeglicherweise gerade" — mehr nicht.

ANSWERED = "ANSWERED"
#: Jemand hat abgehoben. Das ist KEINE Vermutung aus einem 200er, sondern haengt
#: an `accepted_time_unix_secs` des Anbieters (siehe `call_state_from_provider`).

NO_ANSWER = "NO_ANSWER"
BUSY = "BUSY"

VOICEMAIL = "VOICEMAIL"
#: Steht in der Menge, wird von V1 aber NIE gesetzt. Der SOLVIO-Agent hat die
#: Anrufbeantworter-Erkennung des Anbieters ausdruecklich NICHT aktiviert (sie
#: ist ein Werkzeug, und der Agent ist werkzeuglos). Ein Anrufbeantworter, der
#: abnimmt, erscheint deshalb als ANSWERED. Das ist unschoen und ehrlich; die
#: Alternative waere ein geratener Zustand. Siehe DEBT-Eintrag im Register.

COMPLETED = "COMPLETED"
#: Das Gespraech lief und ist regulaer zu Ende. Sagt nichts ueber den Inhalt.

FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

CALL_STATES = frozenset({
    PREPARED, DIALING, ANSWERED, NO_ANSWER, BUSY, VOICEMAIL,
    COMPLETED, FAILED, UNKNOWN,
})

#: Zustaende, nach denen nichts mehr kommt. Alles andere darf weiter gepollt
#: werden — und nur diese hier duerfen einen Ledger-Eintrag abschliessen.
TERMINAL_CALL_STATES = frozenset({
    ANSWERED, NO_ANSWER, BUSY, VOICEMAIL, COMPLETED, FAILED,
})

# ---------------------------------------------------------------------------
# Der Zustand der NACHRICHT — bewusst getrennt
# ---------------------------------------------------------------------------
NOT_DELIVERED = "NOT_DELIVERED"
DELIVERED_BY_AGENT = "DELIVERED_BY_AGENT"
#: Der Agent hat die gebundene Nachricht ausgesprochen. Belegt aus dem
#: Transkript, nicht aus dem Verbindungszustand.

RECIPIENT_ACKNOWLEDGED = "RECIPIENT_ACKNOWLEDGED"
#: Der Empfaenger hat nach der Nachricht selbst gesprochen. Das ist die
#: staerkste Aussage, die ein Transkript hergibt — und sie bedeutet
#: ausdruecklich NICHT "hat zugestimmt" oder "wird es tun".

DELIVERY_UNKNOWN = "UNKNOWN"

MESSAGE_DELIVERY_STATES = frozenset({
    NOT_DELIVERED, DELIVERED_BY_AGENT, RECIPIENT_ACKNOWLEDGED, DELIVERY_UNKNOWN,
})

# ---------------------------------------------------------------------------
# Anbieter-Statuswerte (gemessen an der OpenAPI-Spezifikation, nicht geraten)
# ---------------------------------------------------------------------------
#: Der `status` einer Conversation. Wortlaut aus dem Enum der Anbieterspezifikation.
PROVIDER_STATUS_INITIATED = "initiated"
PROVIDER_STATUS_IN_PROGRESS = "in-progress"
PROVIDER_STATUS_PROCESSING = "processing"
PROVIDER_STATUS_DONE = "done"
PROVIDER_STATUS_FAILED = "failed"

#: `processing` ist die Falle: der Anruf ist vorbei, aber Transkript und Analyse
#: sind es nicht. Wer hier abschliesst, verliert die Ergebniswahrheit.
PROVIDER_STATUS_PENDING = frozenset({
    PROVIDER_STATUS_INITIATED, PROVIDER_STATUS_IN_PROGRESS, PROVIDER_STATUS_PROCESSING,
})
PROVIDER_STATUS_TERMINAL = frozenset({PROVIDER_STATUS_DONE, PROVIDER_STATUS_FAILED})

#: Abbruchgruende des Anbieters, die einen eigenen Verbindungszustand tragen.
#: Wortlaut aus `ConversationErrorType`. Alles, was hier NICHT steht, wird zu
#: FAILED — nie zu etwas Genauerem.
_ERROR_TO_CALL_STATE: dict[str, str] = {
    "line_busy": BUSY,
    "no_answer": NO_ANSWER,
}


def call_state_from_provider(status: str, *, accepted: bool,
                             error_type: str = "") -> str:
    """Uebersetzt den Anbieterbefund in EINEN Verbindungszustand. Vorgabe UNKNOWN.

    Die Reihenfolge der Pruefungen ist die Aussage dieser Funktion:

    1. Ein bekannter Fehlergrund gewinnt immer. `line_busy` ist besetzt, auch
       wenn der Statustext etwas anderes nahelegt.
    2. `accepted` entscheidet ueber abgehoben/nicht abgehoben. Es kommt aus
       `accepted_time_unix_secs` und ist der einzige belastbare Beleg dafuer,
       dass ein Mensch den Hoerer genommen hat. Ein 200er auf den Anrufauftrag
       ist es ausdruecklich nicht.
    3. Erst danach der Statustext.

    Ein unbekannter Statuswert wird UNKNOWN und nicht etwa COMPLETED: der
    Anbieter darf seine Enums erweitern, ohne dass SOLVIO anfaengt zu raten.
    """
    error_type = str(error_type or "").strip().lower()
    if error_type in _ERROR_TO_CALL_STATE:
        return _ERROR_TO_CALL_STATE[error_type]

    status = str(status or "").strip().lower()
    if status in PROVIDER_STATUS_PENDING:
        return DIALING
    if status == PROVIDER_STATUS_FAILED:
        # Ein Fehler NACH dem Abheben ist trotzdem ein Gespraech gewesen; der
        # Unterschied zaehlt fuer die Kostenwahrheit und fuer die Frage, ob die
        # Nachricht ankam.
        return FAILED if not accepted else COMPLETED
    if status == PROVIDER_STATUS_DONE:
        return COMPLETED if accepted else NO_ANSWER
    return UNKNOWN


def delivery_state_from_evidence(state: str, *,
                                 agent_delivered: bool | None,
                                 acknowledged: bool | None) -> str:
    """Wurde die Nachricht ausgerichtet? Fail-closed, mit drei Wahrheitswerten.

    Die beiden Belege sind ausdruecklich DREIWERTIG: `True` heisst belegt,
    `False` heisst widerlegt, `None` heisst **nicht feststellbar**. Der
    Unterschied zwischen den letzten beiden ist der Kern dieser Funktion —
    "wir wissen, dass er es nicht gesagt hat" und "wir wissen es nicht" duerfen
    nicht in derselben Antwort landen.

    Und die Regel, die ueber allem steht:

        `accepted_time_unix_secs != null` beweist AUSSCHLIESSLICH, dass die
        Gegenstelle den Anruf angenommen hat.

    Es beweist nicht, dass die gewuenschte Person am Telefon war, nicht, dass
    ein Mensch statt einer Mailbox abgenommen hat, nicht, dass die Nachricht
    verstanden wurde, und nicht, dass jemand sie bestaetigt hat. Deshalb kommt
    der Verbindungszustand in dieser Funktion nur noch als Ausschlusskriterium
    vor: er kann Zustellung VERNEINEN (nie abgehoben), aber niemals begruenden.
    Ein abgehobener Anruf ohne Transkriptbeleg endet hier bei UNKNOWN.
    """
    if state not in TERMINAL_CALL_STATES:
        return DELIVERY_UNKNOWN
    # Was nie angenommen wurde, hat nichts zugestellt. Das ist der einzige
    # Schluss, den der Verbindungszustand allein tragen darf — und er gilt fuer
    # genau diese drei Zustaende.
    #
    # FAILED gehoert dazu, und das ist keine Nachlaessigkeit: `call_state_from_
    # provider` vergibt FAILED ausschliesslich, wenn `accepted` falsch ist. Ein
    # Fehler NACH dem Abheben wird dort zu COMPLETED, weil dann ein Gespraech
    # stattgefunden hat. Wer hier FAILED sieht, weiss deshalb, dass niemand
    # abgehoben hat.
    if state in (NO_ANSWER, BUSY, FAILED):
        return NOT_DELIVERED
    if agent_delivered is False:
        return NOT_DELIVERED
    if agent_delivered is None:
        # Abgehoben, aber ohne Beleg im Transkript. Genau hier lag die Falle:
        # frueher wurde daraus NOT_DELIVERED, also eine Behauptung.
        return DELIVERY_UNKNOWN
    if acknowledged is True:
        return RECIPIENT_ACKNOWLEDGED
    # `False` wie `None` fuehren bewusst zum selben Ergebnis: belegt ist nur,
    # dass der Agent gesprochen hat. Eine Bestaetigung wird nie unterstellt.
    return DELIVERED_BY_AGENT


# ---------------------------------------------------------------------------
# Evidenz aus dem Transkript
# ---------------------------------------------------------------------------
#: Ab dieser Ueberdeckung der inhaltstragenden Woerter gilt die Nachricht als
#: ausgesprochen. Kein Modellaufruf: die Frage muss aus denselben Daten immer
#: dieselbe Antwort bekommen, sonst ist sie als Beleg wertlos.
_DELIVERED_AT = 0.6

#: Wieviele inhaltstragende Woerter eine Nachricht mindestens haben muss, damit
#: die Messung ueberhaupt etwas aussagt.
#:
#: Der Grund ist ein gemessener Fehlschlag: bei „Bitte nicht vergessen!" bleibt
#: nach dem Stoppwortfilter der Kern {vergessen}, also n=1. Sagt der Agent
#: irgendwo „Entschuldigen Sie, ich habe vergessen mich vorzustellen", betraegt
#: die Ueberdeckung 1.0 — und die Nachricht galt als ausgerichtet, obwohl sie
#: nie ausgesprochen wurde. Bei sehr kleinen Kernen ist eine zufaellige
#: Wortgleichheit kein Beleg, sondern ein Muenzwurf.
_MIN_CORE_WORDS = 4

#: Aus der Wortueberdeckung entsteht KEIN Gegenbeleg mehr — nur noch aus dem
#: Schweigen des Agenten.
#:
#: Hier standen nacheinander 0.2 und 0.0, und beide waren gemessen falsch. Der
#: Agent SOLL die Nachricht natuerlich ausrichten statt sie abzulesen: „Ich
#: komme heute leider spaeter" wird zu „er verspaetet sich und trifft eine
#: halbe Stunde spaeter ein" — inhaltlich genau richtig, Ueberdeckung null,
#: weil kein einziges Kernwort woertlich vorkommt. Eine Schwelle, die das als
#: „nicht ausgerichtet" bucht, bestraft genau das Verhalten, das der
#: Systemprompt verlangt.
#:
#: Zwischen „stark umformuliert" und „am Thema vorbei" kann diese Messung nicht
#: unterscheiden. Also unterscheidet sie es nicht: die Ueberdeckung kann
#: Zustellung nur noch BELEGEN, nie widerlegen. Widerlegt ist sie allein, wenn
#: der Agent ueberhaupt nichts gesagt hat — dort ist es Wissen.

_STOPWORDS = frozenset({
    "der", "die", "das", "und", "ist", "ich", "du", "er", "sie", "es", "wir",
    "ihr", "ein", "eine", "einen", "einem", "einer", "dass", "den", "dem",
    "zu", "im", "in", "an", "am", "auf", "mit", "von", "fuer", "für", "bei",
    "hat", "habe", "haben", "wird", "werden", "war", "sein", "nicht", "auch",
    "noch", "dann", "so", "wie", "als", "aber", "oder", "bitte", "mal",
})


def _words(text: str) -> list[str]:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in str(text or ""))
    return [w for w in cleaned.split() if len(w) > 2 and w not in _STOPWORDS]


def message_evidence(transcript: object, bound_message: str) -> bool | None:
    """Hat der Agent die gebundene Nachricht ausgesprochen? True / False / None.

    Der Agent soll die Nachricht NATUERLICH ausrichten und nicht mechanisch
    ablesen — so steht es in seinem Systemprompt. Eine Wortgleichheitspruefung
    wuerde deshalb regelmaessig das Falsche sagen. Gemessen wird stattdessen,
    wieviel vom inhaltstragenden Kern der Nachricht in den Agentenbeitraegen
    vorkommt.

    `None` bedeutet **nicht feststellbar** und ist der Vorgabewert, wann immer
    die Datenlage duenn ist: kein Transkript, keine Agentenbeitraege, oder eine
    Nachricht ohne pruefbaren Kern. Nach `delivery_state_from_evidence` fuehrt
    das zu UNKNOWN — nie zu einer Behauptung in die eine oder andere Richtung.
    """
    kern = set(_words(bound_message))
    if len(kern) < _MIN_CORE_WORDS:
        # Zu wenig Kern, um irgendetwas zu belegen — in beide Richtungen.
        return None
    if not isinstance(transcript, (list, tuple)) or not transcript:
        return None

    # Je Agentenbeitrag messen, nicht ueber die Vereinigung aller Beitraege.
    # Sonst summiert sich ein langes Gespraech zufaellig zur Ueberdeckung: der
    # Agent haette die Nachricht nie am Stueck gesagt, aber ihre Woerter
    # verstreut ueber zehn Saetze benutzt.
    beste = -1.0
    stumm = True
    for turn in transcript:
        if not isinstance(turn, Mapping):
            continue
        if str(turn.get("role") or "").strip().lower() != "agent":
            continue
        gesagt = set(_words(turn.get("message") or ""))
        if gesagt:
            stumm = False
        beste = max(beste, len(kern & gesagt) / len(kern))
    if beste < 0:
        # Ueberhaupt keine Agentenbeitraege: das Transkript sagt nichts.
        return None
    if stumm:
        # Der Agent kam zu Wort und hat nichts Inhaltliches gesagt. Das ist der
        # EINZIGE Gegenbeleg, den diese Messung hergibt.
        return False
    if beste >= _DELIVERED_AT:
        return True
    return None


def acknowledgement_evidence(transcript: object) -> bool | None:
    """Hat eine menschlich wirkende Gegenstelle die Nachricht bestaetigt?

    In V1 lautet die Antwort **immer** `None`, und das ist eine Entscheidung,
    keine Luecke.

    Der Grund: eine Mailbox spricht. Ihre Ansage wird vom Anbieter als
    Nutzerbeitrag transkribiert, genau wie ein Mensch. Aus "die Gegenstelle hat
    nach der Nachricht gesprochen" folgt deshalb NICHT "ein Mensch hat
    bestaetigt" — und eine Anrufbeantworter-Erkennung hat V1 bewusst nicht, weil
    sie beim Anbieter ein Werkzeug waere und der SOLVIO-Agent werkzeuglos ist.

    Was hier moeglich waere, waere Stichwortraten auf deutschen
    Bestaetigungsfloskeln. Das waere genau die Art von Heuristik, die im
    Zweifel das Angenehme behauptet. Solange keine belastbare native Evidenz
    vorliegt, bleibt die Antwort unbestimmt; die tatsaechliche Aeusserung der
    Gegenstelle wird stattdessen woertlich als `recipient_response`
    weitergereicht — als Information, nicht als Bestaetigung.

    Siehe den Schuldeintrag zur fehlenden Mailbox-Erkennung.
    """
    return None


# ---------------------------------------------------------------------------
# Die Ergebnishuelle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostTruth:
    """Was der Anruf gekostet hat — und was davon belegt ist.

    `fiat` ist die Geldangabe des Anbieters (`cost_fiat`) und darf `None` sein.
    Dass sie fehlt, heisst NICHT "kostenlos": die Telefonieminuten rechnet der
    Netzbetreiber getrennt ab und sie tauchen hier nie auf. `complete` sagt
    genau das — und verhindert, dass jemand diese Zahl fuer die
    Gesamtkosten haelt.
    """

    credits: int | None = None
    fiat: float | None = None
    duration_secs: int | None = None
    complete: bool = False
    note: str = ("Netzentgelte des Telefonanbieters sind hier NICHT enthalten; "
                 "sie werden dort getrennt abgerechnet.")


@dataclass(frozen=True)
class RecipientIdentity:
    """Wen SOLVIO angerufen hat — aus der bestaetigten Bindung, nie aus Modelltext."""

    alias: str
    display_name: str
    recipient_handle: str


@dataclass
class CallResult:
    """Das Ergebnis eines Anrufs. Geschlossen, gepruefte Zustaende, keine Deutung.

    `transcript_summary` und `recipient_response` stammen vom Anbieter bzw. aus
    dem Gespraech. Sie sind INFORMATION und tragen `content_trust`; wer sie
    liest, darf daraus keine Befugnis ableiten — siehe TRUST_BOUNDARY.
    """

    call_id: str
    provider: str
    recipient_identity: RecipientIdentity
    call_state: str
    message_delivery_state: str
    conversation_id: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    recipient_response: str = ""
    transcript_summary: str = ""
    provider_result: Mapping[str, Any] = field(default_factory=dict)
    cost_truth: CostTruth = field(default_factory=CostTruth)
    #: Alles, was aus dem Gespraech stammt, ist Fremdinhalt. Der Wert ist
    #: absichtlich fest verdrahtet und kein Parameter: eine Faehigkeit soll die
    #: Vertrauensklasse ihres eigenen Ergebnisses nicht waehlen koennen.
    #:
    #: Es ist ausdruecklich ein Mitglied von `TrustLevel` und kein eigener
    #: Freitext. Hier stand zuerst `untrusted_external_conversation` — gut
    #: gemeint und wirkungslos: der Wert lag ausserhalb des einzigen Vokabulars,
    #: auf dem `bears_authority()` und die Trust-Map aufsetzen, und war damit
    #: reine Beschriftung. Ein Telefongespraech ist eine fremdverfasste
    #: Nachricht; `UNTRUSTED_MESSAGE` steht bereits in der Menge `UNTRUSTED`
    #: und traegt deshalb tatsaechlich die Regel „kann informieren, nie
    #: autorisieren".
    content_trust: str = TrustLevel.UNTRUSTED_MESSAGE.value

    def __post_init__(self) -> None:
        # Ein Telefongespraech ist fremdverfasst. Punkt.
        #
        # Der Kommentar oben sagte das schon; durchgesetzt hat es niemand.
        # `CallResult(..., content_trust="user_direct")` ging glatt durch, und
        # damit haette ein Anrufergebnis behaupten koennen, es sei eine Aussage
        # des Eigentuemers. Heute setzt das niemand — das ist kein Grund, es
        # moeglich zu lassen.
        if self.content_trust != TrustLevel.UNTRUSTED_MESSAGE.value:
            raise ValueError("content_trust is not settable")
        if self.call_state not in CALL_STATES:
            raise ValueError(f"unknown call state: {self.call_state!r}")
        if self.message_delivery_state not in MESSAGE_DELIVERY_STATES:
            raise ValueError(
                f"unknown delivery state: {self.message_delivery_state!r}")
        # Die eine Kombination, die nie entstehen darf: nicht abgehoben, aber
        # Nachricht angeblich ausgerichtet. Kein `assert` — was unter `python -O`
        # verschwindet, ist keine Wache.
        if (self.call_state in (NO_ANSWER, BUSY)
                and self.message_delivery_state in (DELIVERED_BY_AGENT,
                                                    RECIPIENT_ACKNOWLEDGED)):
            raise ValueError(
                "a call that was never answered cannot have delivered a message")

    @property
    def terminal(self) -> bool:
        return self.call_state in TERMINAL_CALL_STATES


# -- Was der Mensch vor Face ID liest ------------------------------------
#
# Diese beiden Funktionen sind der Grund, warum DEBT-0206 geschlossen werden
# konnte: sie erzeugen den Text, den der Eigentuemer bestaetigt. Sie stehen hier
# und nicht im Anzeigepfad, weil ihr Ergebnis in den Digest wandert — es ist
# Vertrag, keine Kosmetik. Aendert sich diese Formatierung, aendert sich der
# Digest, und eine laufende Freigabe gilt nicht mehr. Das ist beabsichtigt.

#: So viele Endziffern bleiben stehen. Vier ist die Zahl, die auf Rechnungen und
#: Karten ueblich ist, und sie reicht, um zwei Nummern desselben Menschen zu
#: unterscheiden.
_TAIL_DIGITS = 4
#: Und so viele vorne, damit Land und Netz sichtbar bleiben: `+49 151` sagt dem
#: Eigentuemer, dass ein deutsches Mobiltelefon klingelt und kein Auslandsziel.
_HEAD_DIGITS = 5
#: Und so viele muessen mindestens verdeckt bleiben, damit die Maske eine ist.
_MIN_MASKED = 3


def mask_phone(handle: str) -> str:
    """Die Rufnummer, lesbar und trotzdem nicht vollstaendig ausgeschrieben.

    Warum ueberhaupt maskieren: dieser Text wird gespeichert (in der Freigabe)
    und angezeigt (auf dem Sperrbildschirm, wenn eine Mitteilung aufschlaegt).
    Eine vollstaendige Rufnummer eines Dritten gehoert an beide Orte nicht.

    Warum trotzdem Kopf UND Ende: die Maskierung soll eine Verwechslung
    aufdecken, nicht verbergen. `+49 151 ···· 3703` schliesst ein Auslandsziel
    und eine fremde Endziffernfolge aus — zusammen mit dem daneben stehenden
    Namen ist das die Gegenprobe, die der Eigentuemer machen soll.

    Zu kurze oder unerwartet geformte Nummern werden NICHT halb maskiert,
    sondern ganz gezeigt: eine Maske, die mehr verdeckt als sie stehen laesst,
    ist keine Gegenprobe mehr, und eine stille Teilanzeige waere schlimmer als
    eine sichtbare Vollanzeige.
    """
    roh = str(handle or "").strip()
    ziffern = [z for z in roh if z.isdigit()]
    # Mindestens drei verdeckte Ziffern, sonst gar keine Maske. Eine Maske, die
    # eine einzige Ziffer verbirgt, verschleiert nichts und macht den Text nur
    # schwerer lesbar — dann lieber ehrlich die ganze Nummer zeigen.
    if len(ziffern) < _HEAD_DIGITS + _TAIL_DIGITS + _MIN_MASKED:
        return roh
    kopf = "".join(ziffern[:_HEAD_DIGITS])
    ende = "".join(ziffern[-_TAIL_DIGITS:])
    verdeckt = len(ziffern) - _HEAD_DIGITS - _TAIL_DIGITS
    plus = "+" if roh.startswith("+") else ""
    return f"{plus}{kopf} {'·' * verdeckt} {ende}"


def spoken_duration(seconds: int) -> str:
    """„3 Minuten" statt „180" — der Eigentuemer denkt nicht in Sekunden."""
    sek = int(seconds)
    def _sek(n: int) -> str:
        return "1 Sekunde" if n == 1 else f"{n} Sekunden"

    if sek < 60:
        return _sek(sek)
    minuten, rest = divmod(sek, 60)
    wort = "1 Minute" if minuten == 1 else f"{minuten} Minuten"
    return wort if rest == 0 else f"{wort} {_sek(rest)}"
