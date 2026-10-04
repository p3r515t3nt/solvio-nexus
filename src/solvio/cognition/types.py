"""Die Formen, die zwischen Gespraech und Arbeit stehen.

Vier geschlossene Vokabulare und zwei Datensaetze. Mehr ist es nicht, und mehr
soll es nicht sein: **die Routentabelle ist das einzige Vokabular, das der
Router ausfuehren kann.** `memory_*`, `secret_*`, Zahlungsnamen und
`background_*` sind keine Routen — sie sind damit nicht gefiltert, sondern
unerreichbar, und das ist der Unterschied zwischen „wir passen auf" und „es
geht nicht".

`TaskAssessment` traegt ausdruecklich KEIN Autoritaetsfeld. Es gibt kein
`trust`, kein `origin`, kein `approval`, kein `risk`, kein `tier`, kein
`budget`. Das Modell schlaegt vor; was gilt, entscheidet die Politik daneben,
und was es kostet, entscheidet die Matrix von ADR-0022 aus Transportwahrheit.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    """Die acht Wege. Geschlossen — ein neunter waere eine Vertragsaenderung."""

    #: Das ist Gespraech. Das Sprachmodell antwortet selbst.
    HAND_BACK = "kein_auftrag"
    #: Genau eine Rueckfrage, ein Satz.
    CLARIFY = "klaerung"
    #: Einmal gruendlich denken und antworten.
    REASON = "nachdenken"
    #: Eine findbare Einzelfrage — die freigegebene Recherche-Kapsel.
    RESEARCH_QUICK = "kurzrecherche"
    #: Ein Fachbot, lesend und synchron.
    CONSULT = "fachbot"
    #: SOLVIOs eigener Zustand.
    DIAGNOSE = "diagnose"
    #: Ein Auftrag, der laenger dauert als das Gespraech.
    AGENT_RESEARCH = "auftrag_recherche"
    #: Ein Auftrag, der etwas baut oder repariert.
    AGENT_BUILD = "auftrag_bau"


#: Die Menge als Zeichenketten — fuer die Buchpruefung und die Validierung.
ROUTES: frozenset[str] = frozenset(r.value for r in Route)

#: Die Rollen des Botteams. Zweite Wahrheit vermieden: sie kommen aus der
#: Registratur der Bots, nicht aus einer Abschrift.
CONSULT_ROLES: tuple[str, ...] = ("researcher", "project_keeper", "diagnostician")

#: Wie schwer das Modell die Sache findet. Eine EMPFEHLUNG, kein Beschluss.
DIFFICULTIES: frozenset[str] = frozenset({"niedrig", "mittel", "hoch"})


class ModelTier(str, Enum):
    """Zwei Stufen, und die Politik waehlt sie — nie ein Modell."""

    MINI = "mini"
    LARGE = "large"
    #: Es lief gar kein Modell (Dedup, Schleifenzaun, fehlender Broker).
    NONE = "none"


class EscalationEvent(str, Enum):
    """Der geschlossene Ereigniskatalog. Ein Modell kann keines ausloesen."""

    NONE = ""
    #: E1 — die Einschaetzung war zweimal unbrauchbar.
    ASSESSMENT_INVALID = "assessment_invalid"
    #: E2 — gueltig, aber unsicher; oder der eine benannte Widerspruch.
    ASSESSMENT_LOW_CONFIDENCE = "assessment_low_confidence"
    #: E3 — nachdenken bei hoher Schwierigkeit.
    REASON_HARD = "reason_hard"
    #: E4 — die Nachfrage eines Planungsereignisses.
    PLAN_REPAIR = "plan_repair"
    #: E5 — die ZWEITE Nachplanung eines Laufs.
    SECOND_REPLANNING = "second_replanning"


ESCALATION_EVENTS: frozenset[str] = frozenset(e.value for e in EscalationEvent)

#: Was das laufende System in diesem Turn TATSAECHLICH getan hat. Geschlossen.
#: Nur im Schattenmodus belegt — im aktiven Modus ist der Router selbst der
#: Handelnde, und dann gibt es nichts zu vergleichen.
OBSERVED_KINDS: frozenset[str] = frozenset({
    "",           # nicht beobachtet (aktiver Modus, Kommission)
    "direkt",     # das Sprachmodell hat selbst geantwortet, kein Werkzeug
    "faehigkeit", # eine vorhandene Faehigkeit lief
    "auftrag",    # ein Agentenauftrag wurde angelegt
})

#: Wie eine Entscheidung ausgegangen ist. Geschlossen.
OUTCOMES: frozenset[str] = frozenset({
    "dispatched", "handed_back", "clarification", "refused", "failed", "shadow",
})


@dataclass(frozen=True)
class TaskAssessment:
    """Der validierte Vorschlag des Modells. Traegt keine Autoritaet.

    `objective` sind die Worte des Nutzers — getrimmt, nie neu gedichtet. Wenn
    die Ueberlappungsregel greift, steht hier woertlich der Turn-Text; das ist
    der Riegel, der eingeschleusten Fortsetzungsinhalt daran hindert, ein Ziel
    zu lenken.
    """

    route: Route
    objective: str
    consult_role: str = ""
    continuation_of: str = ""
    difficulty: str = "mittel"
    confidence: float = 0.0
    clarification: str = ""
    preference: str = ""
    tier: ModelTier = ModelTier.MINI
    #: Ob die Ueberlappungsregel gegriffen hat. Kein Fehler — aber der
    #: Unterschied zwischen „das Modell hat das Ziel aus den Worten des
    #: Menschen gebildet" und „wir haben seinen Vorschlag verworfen und die
    #: Worte eingesetzt" entscheidet spaeter, ob eine Kurzrecherche ueberhaupt
    #: losgeschickt werden darf.
    objective_replaced: bool = False
    #: Das Ziel, wie das Modell es vorgeschlagen hat — unveraendert, auch wenn
    #: die Ueberlappungsregel es verworfen hat. Es traegt keine Autoritaet und
    #: wird nie ungeprueft benutzt; es steht hier, damit EINE Route es gegen
    #: einen weiteren Umfang messen kann, ohne dass alle anderen Routen ihre
    #: Zielpruefung veraendert bekommen.
    proposed_objective: str = ""
    #: Semantischer Vorschlag, keine Freigabe und kein historischer Zieltext.
    reference_intent: str = ""
    #: Optionales Text-Auftragsprofil; leer erhaelt den bisherigen Routenvertrag.
    task_profile: str = ""


@dataclass(frozen=True)
class RoutingDecision:
    """Eine Zeile im Entscheidungsbuch. Entscheidungen, nie Gedanken.

    Was hier NICHT steht, steht hier mit Absicht nicht: der Aeusserungstext
    (der wohnt im Gespraechsspeicher und wird per `turn_ref` referenziert), die
    Eingabe an den Einschaetzer, seine Rohantwort, jede freie Begruendung. Der
    Schleifenzaun braucht keinen Text, er braucht einen Abdruck.
    """

    decision_id: str
    at: float
    conversation_ref: str = ""
    turn_ref: str = ""
    origin: str = ""
    objective_digest: str = ""
    route_proposed: str = ""
    route_final: str = ""
    consult_role: str = ""
    preference: str = ""
    tier: str = ModelTier.NONE.value
    escalation_event: str = ""
    confidence: float = 0.0
    difficulty: str = ""
    continuity_ref: str = ""
    produced_ref: str = ""
    outcome: str = "failed"
    failure_kind: str = ""
    #: Nur im Schattenmodus belegt: welches Werkzeug das Sprachmodell
    #: tatsaechlich gewaehlt hat. Die Divergenz ist der Vergleich dieser
    #: Spalte mit `route_final` — und sie ist der ganze Zweck der Messphase.
    observed_tool: str = ""
    #: Die ART dessen, was geschah — die Spalte, die „das Modell hat gar nichts
    #: getan" ueberhaupt erst sichtbar macht. Ohne sie war verpasste Arbeit
    #: nicht von „nicht gemessen" zu unterscheiden.
    observed_kind: str = ""
    #: Hat der Turn eine Freigabe gekostet?
    observed_approval: bool = False
    duration_ms: int = 0
    tokens_assessment: int = 0
