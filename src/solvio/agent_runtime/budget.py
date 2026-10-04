"""Budgets sind Spalten, keine Hoffnung — und die Schleifenbremse ist ein Digest.

Alle Startwerte tragen den Vermerk „kalibrieren am Gemessenen". Das ist dieselbe
Prozedur wie bei den Broker-Kappen, die erst nach der Abnahmeprobe auf Messwerte
gezogen wurden: eine Kappe, die nie gemessen wurde, ist eine Behauptung.

Die eigentliche Entscheidung hier ist die **Schleifenbremse**. Ein Agent, der
nicht weiterkommt, versucht dasselbe noch einmal — mit einer minimal anderen
Formulierung, die ihm wie ein neuer Ansatz vorkommt. Gezaehlt wird deshalb nicht
der Wortlaut, sondern ein Digest ueber (Schrittart, Profil, normalisierter
Auftragstext). Der dritte gleichartige Versuch innerhalb eines Laufs wird
verweigert.

Und: wiederholte niederwertige Invocations (Ergebnis unbrauchbar, Ergebnis
identisch zum vorigen) zaehlen **doppelt** auf das Versuchsbudget. Ein
Spezialist, der dreimal dasselbe Nichts liefert, hat nicht drei Versuche
gebraucht, sondern das Budget verbrannt.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from solvio.agent_runtime.store import SCOPE_ACTION, SCOPE_BUILD, SCOPE_RESEARCH, SCOPE_TASK

#: Wie oft dieselbe Sache versucht werden darf, bevor es eine Schleife ist.
LOOP_THRESHOLD = 3

#: Hoechstens zwei Planrevisionen (Architektur §5). Zusammen mit dem einen
#: Erst-Plan sind das drei Planungsereignisse; mit hoechstens einer Nachfrage je
#: Ereignis ergibt das die harte Obergrenze von SECHS Broker-Aufrufen je Lauf.
MAX_PLAN_REVISIONS = 2
MAX_PLANNER_CALLS_PER_EVENT = 2
MAX_PLANNER_CALLS_PER_RUN = (1 + MAX_PLAN_REVISIONS) * MAX_PLANNER_CALLS_PER_EVENT

#: Bewertungsaufrufe je LAUF — insgesamt, nicht je Ereignis.
#:
#: Zwei, und beide Plaetze sind knapp: eine Format-Nachfrage verbraucht denselben
#: zweiten Platz wie eine Neubewertung nach neuem Ergebnis. Es gibt bewusst kein
#: Polling und keinen Aufruf je Takt — wer nach jedem Tick bewertet, bezahlt das
#: Nachdenken pro Sekunde. Ist die Kappe auf, endet der Lauf mit einem ehrlich
#: begrenzten Teilresultat; er erfindet keinen Erfolg und stellt dem Menschen
#: keine technische Standardfrage.
MAX_ASSESSMENT_CALLS_PER_RUN = 2

#: Die EINE gebundene Nacharbeit eines Arbeiterauftrags (N8/C4 §3.5): findet die
#: Abschlusspruefung konkrete, lokal behebbare Luecken, bekommt der native
#: Arbeiter in derselben Sitzung genau einen weiteren Turn — und die Pruefung
#: danach dieselben zwei Bewertungsplaetze noch einmal. Keine Schleife: eins.
MAX_TASK_REWORKS_PER_RUN = 1


def assessment_call_cap(reworks: int) -> int:
    """Bewertungsaufrufe je Lauf: die Grundkappe plus dieselbe Kappe je
    tatsaechlich eingeleiteter Nacharbeit (hoechstens `MAX_TASK_REWORKS_PER_RUN`)."""
    return MAX_ASSESSMENT_CALLS_PER_RUN * (1 + max(0, min(int(reworks), MAX_TASK_REWORKS_PER_RUN)))


def research_assessment_call_cap(refinements: int) -> int:
    """One extra assessment per durably started research refinement, at most two.

    Format repairs and failed calls consume the same places. Exhausting the
    current cap cannot itself authorize another refinement to create a place.
    """
    return MAX_ASSESSMENT_CALLS_PER_RUN + max(0, min(int(refinements), MAX_PLAN_REVISIONS))


@dataclass(frozen=True)
class Budget:
    """Der Rand eines Laufs. Startwerte, kalibrieren am Gemessenen."""

    max_steps: int
    max_specialist_invocations: int
    seconds: float
    max_attempts_per_step: int = 2
    max_plan_revisions: int = MAX_PLAN_REVISIONS

    def as_dict(self) -> dict:
        return {"max_steps": self.max_steps,
                "max_specialist_invocations": self.max_specialist_invocations,
                "seconds": self.seconds,
                "max_attempts_per_step": self.max_attempts_per_step,
                "max_plan_revisions": self.max_plan_revisions}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Budget":
        data = dict(raw or {})
        base = DEFAULTS[SCOPE_RESEARCH]
        return cls(
            max_steps=int(data.get("max_steps", base.max_steps)),
            max_specialist_invocations=int(
                data.get("max_specialist_invocations", base.max_specialist_invocations)),
            seconds=float(data.get("seconds", base.seconds)),
            max_attempts_per_step=int(
                data.get("max_attempts_per_step", base.max_attempts_per_step)),
            max_plan_revisions=int(
                data.get("max_plan_revisions", base.max_plan_revisions)))


#: Startwerte je Scope (Architektur §14). Bewusst klein — gegen das
#: Schwarm-Theater und den gemessenen RAM-Druck des Mac mini (DEBT-0055).
DEFAULTS: dict[str, Budget] = {
    # Ein Arbeiterschritt plus die eine Nacharbeit (N8/C4 §3.5); keine Planrevision.
    SCOPE_TASK: Budget(max_steps=1 + MAX_TASK_REWORKS_PER_RUN,
                       max_specialist_invocations=1 + MAX_TASK_REWORKS_PER_RUN,
                       seconds=45 * 60, max_plan_revisions=0),
    SCOPE_ACTION: Budget(max_steps=12, max_specialist_invocations=0,
                         seconds=15 * 60),
    SCOPE_RESEARCH: Budget(max_steps=12, max_specialist_invocations=6,
                           seconds=45 * 60),
    SCOPE_BUILD: Budget(max_steps=24, max_specialist_invocations=10,
                        seconds=4 * 3600),
}

#: Hoechstens EIN aktiv arbeitender Lauf. Konfigurierbar bis 2; gezaehlt werden
#: nur aktive, nie parkende (ein wartender Lauf verstopft die Laufzeit nicht).
MAX_CONCURRENT_RUNS = 1

#: Hoechstens EIN Builder-Unterprozess im ganzen System.
MAX_CONCURRENT_BUILDERS = 1


class BudgetExhausted(RuntimeError):
    """Ein Rand ist erreicht. Der Lauf endet ehrlich, statt weiterzulaufen."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}:{detail}" if detail else category)
        #: Eine Fehlerkategorie aus der geschlossenen Liste des Ledgers.
        self.category = category
        self.detail = detail


_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def attempt_digest(kind: str, profile: str, text: str) -> str:
    """Der Fingerabdruck eines Versuchs.

    Normalisiert wird bewusst grob: Kleinschreibung, Satzzeichen weg,
    Leerraum zusammengezogen. Ein Agent, der beim zweiten Versuch ein
    „bitte" einfuegt oder anders umbricht, hat nichts anderes versucht — und
    genau daran scheitert eine Gleichheitspruefung auf dem Wortlaut.
    """
    normalised = _PUNCTUATION.sub(" ", (text or "").lower())
    normalised = _WHITESPACE.sub(" ", normalised).strip()
    seed = f"{kind}|{profile}|{normalised}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:20]


@dataclass
class BudgetLedger:
    """Die laufende Buchhaltung EINES Laufs. Fluechtig; die dauerhafte Wahrheit
    steht im Agent Run Ledger."""

    budget: Budget
    started_at: float = field(default_factory=time.time)
    #: Bereits atomar gebuchte Provider-Wartezeit, kein neues Zeitbudget.
    paused_seconds: float = 0.0
    steps: int = 0
    specialist_invocations: int = 0
    planner_calls: int = 0
    plan_revisions: int = 0
    #: Digest → wie oft schon versucht.
    attempts: dict[str, int] = field(default_factory=dict)
    #: Digest → wie oft das Ergebnis unbrauchbar war (zaehlt doppelt).
    low_value: dict[str, int] = field(default_factory=dict)

    # -- Schritte ------------------------------------------------------

    def check_step(self, *, now: float = 0.0) -> None:
        moment = now or time.time()
        if self.steps >= self.budget.max_steps:
            raise BudgetExhausted("budget_exhausted", "max_steps")
        if moment - self.started_at - self.paused_seconds > self.budget.seconds:
            raise BudgetExhausted("timeout", "wall_clock")

    def note_step(self) -> None:
        self.steps += 1

    # -- Spezialisten --------------------------------------------------

    def check_specialist(self) -> None:
        if self.specialist_invocations >= self.budget.max_specialist_invocations:
            raise BudgetExhausted("budget_exhausted", "max_specialist_invocations")

    def note_specialist(self) -> None:
        self.specialist_invocations += 1

    # -- Planer --------------------------------------------------------

    def check_planner(self) -> None:
        """Die harte Obergrenze. Sie ist nicht dieselbe Frage wie die
        Revisionszahl: eine schema-ungueltige Antwort kostet einen Aufruf, ohne
        eine Revision zu sein."""
        if self.planner_calls >= MAX_PLANNER_CALLS_PER_RUN:
            raise BudgetExhausted("budget_exhausted", "max_planner_calls")

    def note_planner_call(self) -> None:
        self.planner_calls += 1

    def check_revision(self) -> None:
        if self.plan_revisions >= self.budget.max_plan_revisions:
            raise BudgetExhausted("budget_exhausted", "max_plan_revisions")

    def note_revision(self) -> None:
        self.plan_revisions += 1

    # -- Die Schleifenbremse -------------------------------------------

    def guard_attempt(self, kind: str, profile: str, text: str) -> str:
        """Verweigert den dritten gleichartigen Versuch. Liefert den Digest.

        Gezaehlt wird VOR dem Versuch: wer erst hinterher zaehlt, hat den
        dritten schon gemacht.
        """
        digest = attempt_digest(kind, profile, text)
        seen = self.attempts.get(digest, 0) + self.low_value.get(digest, 0)
        if seen >= LOOP_THRESHOLD - 1:
            raise BudgetExhausted("loop_detected", digest)
        if self.attempts.get(digest, 0) >= self.budget.max_attempts_per_step:
            raise BudgetExhausted("budget_exhausted", "max_attempts_per_step")
        self.attempts[digest] = self.attempts.get(digest, 0) + 1
        return digest

    def release_attempt(self, digest: str) -> None:
        """Ein bewiesener Anbieter-Nichtstart (kein Turn, 0 ct) ist kein Versuch:
        die Schleifenbremse zaehlt GLEICHARTIGE VERSUCHE, und zweimal Parken plus
        Fortsetzen durch den Owner endete sonst als `loop_detected`, ohne dass der
        Anbieter ein drittes Mal gerufen wurde (Review Runde 12, W12-1)."""
        if self.attempts.get(digest, 0) > 0:
            self.attempts[digest] -= 1
            if self.attempts[digest] == 0:
                del self.attempts[digest]

    def note_low_value(self, digest: str) -> None:
        """Ein Ergebnis, das nichts hinzufuegt, zaehlt doppelt.

        Sonst kann ein Spezialist das Budget dreimal fuer dasselbe Nichts
        ausgeben und der Lauf sieht aus, als haette er gearbeitet.
        """
        self.low_value[digest] = self.low_value.get(digest, 0) + 1

    def remaining_seconds(self, *, now: float = 0.0) -> float:
        return max(0.0, self.budget.seconds -
                   ((now or time.time()) - self.started_at - self.paused_seconds))
