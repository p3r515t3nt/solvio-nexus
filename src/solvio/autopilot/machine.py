"""Die Zustandsmaschine — sie entscheidet, das Ledger fuehrt Buch.

Zwei Dinge macht dieses Modul, und nur diese zwei:

1. **Kanten.** Was von wo nach wo darf, steht in einer Tabelle. Was nicht in
   der Tabelle steht, wirft. Dieselbe Bauart wie im Agent Run Ledger, aus
   demselben Grund: eine Zustandsmaschine, deren Kanten im Code verstreut
   sind, hat keine Kanten, sondern Gewohnheiten.

2. **Voraussetzungen.** Eine Kante darf erlaubt sein und die Bedingung
   trotzdem nicht erfuellt. Die wichtigste Bedingung ist `-> READY`, und sie
   ist der Grund, warum es diesen Milestone gibt:

       Ein rotes Gate kann durch KEINE Modellmeinung READY werden.

   Deshalb prueft `can_ready()` die Evidence VOR dem Urteil und gibt das
   Urteil selbst nur als LETZTE der vier Bedingungen an. Ein Technical Lead,
   der READY sagt, ist notwendig — nie hinreichend.

Was hier ausdruecklich NICHT wohnt: die Entscheidung, WOHIN als naechstes
gegangen wird. Die trifft der Treiber aus dem TL-Urteil. Dieses Modul sagt
nur, ob ein gewuenschter Schritt zulaessig ist.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solvio.autopilot import store as S
from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Die Kantentabelle (Vertrag §3). Geschlossen.
#:
#: `HUMAN_REQUIRED` und `BLOCKED` haben ihre Rueckkanten NICHT hier stehen,
#: sondern in `_UNPARK`: wohin sie zurueckfuehren, haengt vom gemerkten
#: Zustand ab. Eine Tabelle, die „irgendwohin zurueck" erlaubt, waere keine.
_EDGES: dict[str, frozenset[str]] = {
    S.PLANNING:  frozenset({S.BUILDING, S.HUMAN_REQUIRED, S.BLOCKED, S.STOPPED}),
    S.BUILDING:  frozenset({S.TESTING, S.HUMAN_REQUIRED, S.BLOCKED, S.STOPPED}),
    S.TESTING:   frozenset({S.REVIEWING, S.BLOCKED, S.STOPPED}),
    S.REVIEWING: frozenset({S.READY, S.FIXING, S.BUILDING, S.HUMAN_REQUIRED,
                            S.BLOCKED, S.STOPPED}),
    S.FIXING:    frozenset({S.TESTING, S.HUMAN_REQUIRED, S.BLOCKED, S.STOPPED}),
    S.BLOCKED:   frozenset({S.PLANNING, S.BUILDING, S.FIXING, S.STOPPED}),
    S.HUMAN_REQUIRED: frozenset({S.PLANNING, S.BUILDING, S.TESTING,
                                 S.REVIEWING, S.FIXING, S.STOPPED}),
    S.READY:     frozenset(),
    S.STOPPED:   frozenset(),
}

#: Aus welchen Zustaenden eine Parkposition zurueckfuehren darf. Der Treiber
#: merkt sich beim Parken den Vorzustand; hierher darf er zurueck.
_UNPARK = frozenset({S.PLANNING, S.BUILDING, S.TESTING, S.REVIEWING, S.FIXING})

#: `TESTING` ist absichtlich ohne Kante nach `HUMAN_REQUIRED`. Ein Test-Gate
#: laeuft oder laeuft nicht; ein rotes Gate ist ein Befund fuer den Technical
#: Lead, keine Frage an den Menschen. Genau das verlangt der Auftrag:
#: „Normale Bugs, rote Tests ... sind KEIN HUMAN_REQUIRED."
NO_HUMAN_FROM = frozenset({S.TESTING})


class TransitionRefused(RuntimeError):
    """Ein Uebergang, der nicht stattfindet. Mit Grund, nicht mit Achselzucken."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ReadyCheck:
    """Warum READY geht oder nicht — vier Bedingungen, einzeln benannt."""

    gate_green: bool
    gate_evidence: str
    acceptance_proven: int
    acceptance_total: int
    blocking_findings: tuple[str, ...]
    lead_verdict: str

    @property
    def ok(self) -> bool:
        return (self.gate_green
                and self.acceptance_total > 0
                and self.acceptance_proven == self.acceptance_total
                and not self.blocking_findings
                and self.lead_verdict == "READY")

    def reason(self) -> str:
        """Der ERSTE unerfuellte Grund — in der Reihenfolge der Wichtigkeit.

        Evidence zuerst, Urteil zuletzt. Wer die Reihenfolge umdreht, meldet
        bei rotem Gate „kein READY-Urteil" und verschweigt das rote Gate.
        """
        if not self.gate_green:
            return "gate_not_green"
        if self.acceptance_total == 0:
            return "no_acceptance_criteria"
        if self.acceptance_proven != self.acceptance_total:
            return (f"acceptance_incomplete:"
                    f"{self.acceptance_proven}/{self.acceptance_total}")
        if self.blocking_findings:
            return f"blocking_findings:{len(self.blocking_findings)}"
        if self.lead_verdict != "READY":
            return f"no_ready_verdict:{self.lead_verdict or 'none'}"
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"gate_green": self.gate_green,
                "gate_evidence": self.gate_evidence,
                "acceptance_proven": self.acceptance_proven,
                "acceptance_total": self.acceptance_total,
                "blocking_findings": list(self.blocking_findings),
                "lead_verdict": self.lead_verdict,
                "ok": self.ok, "reason": self.reason()}


def allowed(current: str, target: str) -> bool:
    """Steht die Kante in der Tabelle? Reine Auskunft, ohne Nebenwirkung."""
    if current not in _EDGES or target not in S.ALL_STATES:
        return False
    return target in _EDGES[current]


def ready_check(ledger: S.AutopilotLedger, milestone_id: str, *,
                gate_evidence_id: str = "", lead_verdict: str = "") -> ReadyCheck:
    """Die vier Bedingungen fuer READY, aus dem Ledger gemessen.

    `gate_evidence_id` leer heisst: es gibt keine, also ist das Gate nicht
    gruen. Fehlende Evidence ist kein Zweifelsfall, sondern ein Nein.
    """
    beleg = ledger.evidence(gate_evidence_id) if gate_evidence_id else None
    gruen = bool(beleg and beleg.ok and beleg.kind == "test_report")
    bewiesen, gesamt = ledger.acceptance_counts(milestone_id)
    blocker = tuple(f.finding_id for f in ledger.blocking_findings(milestone_id))
    return ReadyCheck(gate_green=gruen,
                      gate_evidence=gate_evidence_id if gruen else "",
                      acceptance_proven=bewiesen, acceptance_total=gesamt,
                      blocking_findings=blocker, lead_verdict=lead_verdict)


def transition(ledger: S.AutopilotLedger, milestone_id: str, target: str, *,
               summary: str = "", gate_evidence_id: str = "",
               lead_verdict: str = "", block_reason: str = "",
               resume_at: float = 0.0, now: float = 0.0) -> str:
    """Fuehrt einen Uebergang aus — oder verweigert ihn mit Grund.

    Die Reihenfolge der Pruefungen ist Absicht: erst die Kante (gibt es diesen
    Weg ueberhaupt?), dann die Voraussetzung (darf er heute gegangen werden?).
    Wer beides vermischt, bekommt Fehlermeldungen, die nicht sagen, welches
    von beidem fehlte.
    """
    zustand = ledger.milestone(milestone_id)
    aktuell = zustand.state

    if aktuell in S.TERMINAL_STATES:
        raise TransitionRefused("terminal_state", aktuell)
    if target not in S.ALL_STATES:
        raise TransitionRefused("unknown_state", target)

    # -- Parkpositionen: zurueck nur dorthin, wo der Lauf herkam -------------
    if aktuell in S.PARKED_STATES and target not in (S.STOPPED,):
        if ledger.open_boundaries(milestone_id) and aktuell == S.HUMAN_REQUIRED:
            raise TransitionRefused("boundary_still_open", milestone_id)
        gemerkt = zustand.state_before_park
        if target != gemerkt:
            raise TransitionRefused("unpark_target_mismatch",
                                    f"{target} != {gemerkt or 'unbekannt'}")
        if gemerkt not in _UNPARK:
            raise TransitionRefused("bad_parked_origin", gemerkt or "leer")
    elif not allowed(aktuell, target):
        raise TransitionRefused("edge_not_allowed", f"{aktuell}->{target}")

    if target == S.HUMAN_REQUIRED and aktuell in NO_HUMAN_FROM:
        # Ein rotes Gate ist ein Befund, keine Frage an den Menschen.
        raise TransitionRefused("no_human_boundary_from", aktuell)

    # -- Voraussetzungen ------------------------------------------------------
    if target == S.READY:
        pruefung = ready_check(ledger, milestone_id,
                               gate_evidence_id=gate_evidence_id,
                               lead_verdict=lead_verdict)
        if not pruefung.ok:
            raise TransitionRefused("ready_preconditions", pruefung.reason())

    if target == S.REVIEWING and aktuell == S.TESTING:
        beleg = ledger.evidence(gate_evidence_id) if gate_evidence_id else None
        if beleg is None or beleg.kind != "test_report":
            raise TransitionRefused("no_test_evidence", milestone_id)
        if beleg.commit != zustand.last_commit or not beleg.commit:
            # Evidence, die zu einem anderen Commit gehoert, beschreibt einen
            # anderen Baum. Sie darf keinen Review tragen.
            raise TransitionRefused(
                "stale_test_evidence",
                f"{beleg.commit[:12] or 'leer'} != {zustand.last_commit[:12] or 'leer'}")

    if target == S.BLOCKED:
        if block_reason not in S.BLOCK_REASONS:
            raise TransitionRefused("unknown_block_reason", block_reason or "leer")

    if target == S.HUMAN_REQUIRED and not ledger.open_boundaries(milestone_id):
        # HUMAN_REQUIRED ohne offene Grenze waere ein Wartezustand ohne Frage —
        # der Mensch wuesste nicht, was er tun soll.
        raise TransitionRefused("no_open_boundary", milestone_id)

    # -- Ausfuehren -----------------------------------------------------------
    felder: dict[str, Any] = {}
    if target in S.PARKED_STATES:
        felder["state_before_park"] = aktuell
    elif aktuell in S.PARKED_STATES:
        felder["state_before_park"] = ""
    if target == S.BLOCKED:
        felder["block_reason"] = block_reason
        felder["resume_at"] = float(resume_at)
    elif aktuell == S.BLOCKED:
        felder["block_reason"] = ""
        felder["resume_at"] = 0.0
    if felder:
        ledger.set_fields(milestone_id, now=now, **felder)
    ledger.set_state(milestone_id, target, summary=summary, now=now)
    log.info("autopilot.transition", milestone=milestone_id,
             frm=aktuell, to=target)
    return target


def park(ledger: S.AutopilotLedger, milestone_id: str, *, category: str,
         kind: str, question: str, now: float = 0.0) -> str:
    """Eine echte Nutzergrenze oeffnen und den Milestone dorthin parken.

    Erst die Grenze, dann der Zustandswechsel — sonst stuende der Milestone
    kurzzeitig in HUMAN_REQUIRED ohne Frage, und genau das verbietet die
    Voraussetzung oben.
    """
    boundary_id = ledger.open_boundary(milestone_id, category=category,
                                       kind=kind, question=question, now=now)
    transition(ledger, milestone_id, S.HUMAN_REQUIRED,
               summary=f"{category}/{kind}", now=now)
    return boundary_id
