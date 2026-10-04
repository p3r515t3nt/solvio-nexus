"""Die EINE Stelle, an der Stufen, Schwellen und Fristen des Routers stehen.

„AI providers and agent runtimes must remain replaceable" ist eine LOCKED
ARCHITECTURE DECISION. Ein Anbieterwechsel ist deshalb eine Aenderung an dieser
Datei plus der Broker-Konfiguration — nicht eine Suche quer durch das Paket.

Die Modellnamen werden **referenziert, nicht abgeschrieben**: sie stehen im
Broker, weil dort das Modelltor sitzt. Zwei Schreibweisen desselben Namens
waeren die Zwei-Wahrheiten-Krankheit im Kleinen.

**Die Zahlen hier sind Startwerte mit Kalibrierungsauftrag.** Eine Schwelle, die
nie gemessen wurde, ist eine Behauptung; sie steht hier beisammen, damit sie
nicht still dauerhaft wird (DEBT-0149).
"""
from __future__ import annotations

from solvio.cognition.types import ModelTier
from solvio.provider_broker.proxy import LARGE_MODEL, MINI_MODEL
from solvio.provider_broker.service import (COGNITION_ESCALATION_PRINCIPAL,
                                            COGNITION_PRINCIPAL)

#: Stufe → Modell. Der Router kennt nur diese Abbildung; einen Namen aus einer
#: Modellantwort zu uebernehmen ist strukturell nicht vorgesehen.
MODEL_FOR_TIER: dict[ModelTier, str] = {
    ModelTier.MINI: MINI_MODEL,
    ModelTier.LARGE: LARGE_MODEL,
}

#: Stufe → Auftraggeber. Der Token des grossen Auftraggebers liegt nur in
#: Core-Code; ein Kaefig sieht ihn nie.
PRINCIPAL_FOR_TIER: dict[ModelTier, str] = {
    ModelTier.MINI: COGNITION_PRINCIPAL,
    ModelTier.LARGE: COGNITION_ESCALATION_PRINCIPAL,
}

# -- Schwellen ---------------------------------------------------------------

#: Unter dieser Zuversicht wird EINMAL eskaliert (E2). Startwert.
ESCALATE_BELOW = 0.4

#: Wie viel des Ziels aus den Worten des Nutzers stammen muss. Darunter wird
#: das Ziel durch den woertlichen Turn-Text ERSETZT — der Einschaetzer darf die
#: Worte des Nutzers kuerzen, nie neue erfinden. Startwert.
OBJECTIVE_OVERLAP_MIN = 0.7

#: Inhaltstoken ab dieser Laenge zaehlen bei der Ueberlappung. Dieselbe Regel
#: wie im Provenienz-Tor (`capabilities/invocation.py`) — zwei Zaehlweisen fuer
#: dieselbe Frage waeren zwei Wahrheiten.
MIN_TOKEN_LENGTH = 3

# -- Fristen und Kappen ------------------------------------------------------

#: Frist je Modellaufruf des Routers. Die beauftragte Faehigkeit behaelt ihre
#: eigene, freigegebene Frist — der Router bricht NIE etwas ab, das laeuft.
ASSESS_TIMEOUT = 10.0

#: Die gesamte Router-Phase (Dedup + Einschaetzung + Politik).
COMMISSION_TIMEOUT = 12.0

#: Lease-Dauer je Aufruf. Wie beim Planer.
LEASE_SECONDS = 120.0

#: Angeheftete Ausgabekappen. Ohne sie veranschlagt der Broker pauschal 4096
#: Ausgabe-Token (`DEFAULT_OUTPUT_ESTIMATE`) — und ein abgebrochener Aufruf
#: bucht diese Schaetzung fuer immer.
MAX_ASSESS_OUTPUT_TOKENS = 600
MAX_REASON_OUTPUT_TOKENS = 2_000

#: Hoechstens so viele Einschaetzungen am Tag im Schattenmodus. Darueber wird
#: still uebersprungen: das ist eine Messung, kein Produkt.
SHADOW_MAX_PER_DAY = 100

#: Der Schleifenzaun: so viele gescheiterte Entscheidungen mit demselben
#: Abdruck in diesem Fenster, und die naechste wird OHNE Modellaufruf abgelehnt.
LOOP_WINDOW_SECONDS = 60 * 60.0
LOOP_FAILED_BEFORE_REFUSAL = 2

# -- Kontext -----------------------------------------------------------------

#: Wie viele Arbeitseintraege der Einschaetzer sieht.
REGISTER_MAX = 5

#: Wie lang eine Ergebniszeile im Arbeitsregister sein darf.
REGISTER_SUMMARY_CHARS = 160

#: Der freigegebene Gespraechsausschnitt (`ConversationStore.recent_context`).
RECENT_CONTEXT_CHARS = 3_000

#: Wie weit zurueck der Router im Gespraechsspeicher nach dem vorigen
#: Nutzer-Turn sucht. Seit dieser IMMER bindet, laeuft der Leseweg in jedem
#: Turn — ohne Kappe waere das ein voller Tabellenlauf je gesprochenem Satz.
#: Vierzig Nachrichten decken den gemessenen Vorfall (22 Zeilen) mit Abstand;
#: Startwert mit Kalibrierungsauftrag wie die anderen Zahlen hier.
TURN_LOOKBACK = 40

#: Obergrenze der gesamten Einschaetzer-Eingabe. Das Register wird zuerst
#: gekuerzt — es ist der einzige Teil, der wachsen kann.
MAX_PROMPT_CHARS = 6_000

#: Wie lang die eine Rueckfrage werden darf, bevor sie gekappt wird.
MAX_CLARIFICATION_CHARS = 240
