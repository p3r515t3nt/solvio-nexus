"""Autopilot A1 — Contract, Ledger und Zustandsmaschine.

Die Suite haelt drei Zusicherungen, und die erste ist der Grund fuer den
ganzen Milestone:

**Ein rotes Gate kann durch keine Modellmeinung READY werden.** Nicht „soll
nicht", sondern „kann nicht": die Voraussetzung wird VOR dem Urteil geprueft,
und der gemeldete Grund nennt das Gate, nicht das fehlende Urteil.

**Der Contract gehoert dem Core.** Ein Vorschlag, der sich Produktion erlaubt
oder die Freigabeautoritaet umschreibt, wird beim Parsen abgewiesen — nicht
spaeter beim Ausfuehren.

**Evidence ist an Commit UND Umgebung gebunden.** Beim Offsite-Release fielen
14 Zusicherungen, weil eine frische Umgebung ohne Extras lief; das sah wie ein
Codefehler aus und war keiner. Deshalb ist die Umgebung Teil der
Evidence-Identitaet.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a1-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.autopilot import contract as C   # noqa: E402
from solvio.autopilot import machine as M    # noqa: E402
from solvio.autopilot import store as S      # noqa: E402

BASIS = {
    "milestone_id": "probe-milestone",
    "version": "1.0.0",
    "objective": "Ein kleiner Beweis.",
    "acceptance_criteria": [
        {"key": "gate", "text": "Gate gruen", "evidence_type": "DETERMINISTIC"},
        {"key": "lesbar", "text": "Bericht lesbar",
         "evidence_type": "REVIEW_SUPPORTED"},
    ],
}


# --------------------------------------------------------------------- Werkzeug
def _ledger() -> S.AutopilotLedger:
    """Je Test eine eigene Datei. Eine geteilte Datei prueft ab dem zweiten
    Test die Reihenfolge mit."""
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/autopilot.sqlite3"
    return S.AutopilotLedger(pfad)


def _welt(**aenderung):
    led = _ledger()
    roh = {**BASIS, **aenderung}
    vertrag = C.parse(roh)
    led.create_milestone(vertrag)
    return led, vertrag


def _bis_review(led, vertrag, *, gate_ok: bool, commit: str = "c1"):
    """Der normale Weg bis REVIEWING, mit einem Gate-Ergebnis nach Wahl."""
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    led.set_fields(mid, last_commit=commit)
    M.transition(led, mid, S.TESTING)
    beleg = led.record_evidence(mid, kind="test_report", commit=commit,
                                env_fingerprint="fp1", ok=gate_ok,
                                summary="2944/2944" if gate_ok else "1 rot")
    M.transition(led, mid, S.REVIEWING, gate_evidence_id=beleg)
    return mid, beleg


# ------------------------------------------------- der Contract gehoert dem Core
def t_a_contract_cannot_permit_itself_production() -> None:
    """Selbstermaechtigung faellt beim Parsen, nicht beim Ausfuehren.

    Wer sie erst beim Ausfuehren abfaengt, hat einen Contract im Ledger, der
    etwas Verbotenes verspricht — und irgendwann liest ihn jemand als Zusage.
    """
    for aktion in ("deploy", "merge_to_main", "push_remote", "grant_authority",
                   "disable_gate"):
        exc = require_raises(C.ContractError, C.parse,
                             {**BASIS, "permitted_actions": [aktion]})
        require_equal(exc.reason, "permits_forbidden_action",
                      f"{aktion} wurde mit falschem Grund abgewiesen")


def t_a_contract_cannot_rewrite_its_release_authority() -> None:
    """Die Freigabe gehoert dem Eigentuemer — auch im Contract."""
    exc = require_raises(C.ContractError, C.parse,
                         {**BASIS, "release_authority": "autopilot"})
    require_equal(exc.reason, "bad_release_authority", "falscher Grund")
    ok = C.parse(BASIS)
    require_equal(ok.release_authority, C.RELEASE_AUTHORITY_OWNER,
                  "der Standard ist nicht der Eigentuemer")


def t_an_acceptance_criterion_needs_a_known_evidence_type() -> None:
    """Ohne Evidence-Typ waere Amendment 9 eine Bitte statt einer Regel."""
    exc = require_raises(
        C.ContractError, C.parse,
        {**BASIS, "acceptance_criteria": [
            {"key": "a", "text": "t", "evidence_type": "VIBES"}]})
    require_equal(exc.reason, "unknown_evidence_type", "falscher Grund")


def t_the_pinned_contract_is_the_truth_not_the_file() -> None:
    """Ein geaenderter Vorschlag ist ein Vorschlag — und wird erkannt.

    Der gefaehrlichste Fall ist NICHT die neue Fassung, sondern dieselbe
    Fassungsnummer mit anderem Inhalt: die sieht in jeder Uebersicht
    unveraendert aus.
    """
    kanonisch = C.parse(BASIS)
    require_equal(C.differs(kanonisch, BASIS), "",
                  "identischer Inhalt galt als Abweichung")

    getarnt = {**BASIS, "objective": "Etwas ganz anderes."}
    require_equal(C.differs(kanonisch, getarnt), "same_version_different_content",
                  "eine getarnte Aenderung wurde nicht erkannt")

    neu = {**BASIS, "version": "1.1.0"}
    require_equal(C.differs(kanonisch, neu), "new_version_proposed",
                  "eine neue Fassung wurde nicht als solche erkannt")


def t_the_projection_a_builder_sees_is_read_only() -> None:
    """Die Projektion traegt ihren Hash — der Empfaenger kann keine andere
    Fassung meinen, ohne dass es auffaellt."""
    vertrag = C.parse(BASIS)
    sicht = vertrag.projection()
    require(sicht["readonly"] is True, "die Projektion ist nicht als read-only markiert")
    require_equal(sicht["contract_hash"], vertrag.digest(),
                  "die Projektion traegt einen anderen Hash als der Contract")
    require("CONTRACT_CHANGE_REQUIRED" in sicht["hinweis"],
            "die Projektion sagt nicht, wie eine Aenderung geht")


# ------------------------------------------------------- das Gate schlaegt alles
def t_a_red_gate_cannot_be_talked_into_ready() -> None:
    """DIE Zusicherung dieses Milestones.

    Geprueft wird nicht, dass ein READY-Urteil fehlt, sondern dass es NICHTS
    aendert. Beide Urteile muessen an derselben Bedingung scheitern, und der
    gemeldete Grund muss das GATE nennen — nicht das Urteil.
    """
    led, vertrag = _welt()
    mid, rot = _bis_review(led, vertrag, gate_ok=False)
    # Auch mit vollstaendiger Akzeptanz und ohne Findings.
    for schluessel in ("gate", "lesbar"):
        led.set_criterion(mid, schluessel, state=S.CRIT_PROVEN, evidence_ref=rot)

    for urteil in ("READY", "FIX", ""):
        exc = require_raises(M.TransitionRefused, M.transition, led, mid,
                             S.READY, gate_evidence_id=rot, lead_verdict=urteil)
        require_equal(exc.reason, "ready_preconditions", "falsche Ablehnung")
        require_equal(exc.detail, "gate_not_green",
                      f"bei Urteil {urteil!r} wurde nicht das Gate genannt")
    require_equal(led.milestone(mid).state, S.REVIEWING,
                  "der Milestone ist trotz Ablehnung weitergerutscht")


def t_ready_needs_all_four_conditions() -> None:
    """Vier Bedingungen, einzeln gestellt — jede einzeln toedlich."""
    led, vertrag = _welt()
    mid, gruen = _bis_review(led, vertrag, gate_ok=True)

    # 1. Akzeptanz unvollstaendig
    led.set_criterion(mid, "gate", state=S.CRIT_PROVEN, evidence_ref=gruen)
    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.READY,
                         gate_evidence_id=gruen, lead_verdict="READY")
    require("acceptance_incomplete:1/2" in exc.detail,
            f"unvollstaendige Akzeptanz nicht benannt: {exc.detail}")

    # 2. blockierendes Finding
    led.set_criterion(mid, "lesbar", state=S.CRIT_PROVEN, evidence_ref=gruen)
    fid = led.open_finding(mid, severity="major", title="Etwas Wichtiges")
    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.READY,
                         gate_evidence_id=gruen, lead_verdict="READY")
    require("blocking_findings" in exc.detail,
            f"das offene Finding wurde nicht benannt: {exc.detail}")

    # 3. kein READY-Urteil
    led.close_finding(fid)
    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.READY,
                         gate_evidence_id=gruen, lead_verdict="FIX")
    require("no_ready_verdict" in exc.detail,
            f"das fehlende Urteil wurde nicht benannt: {exc.detail}")

    # 4. alles erfuellt -> und erst JETZT geht es
    M.transition(led, mid, S.READY, gate_evidence_id=gruen, lead_verdict="READY")
    require_equal(led.milestone(mid).state, S.READY,
                  "bei erfuellten Bedingungen ging READY nicht")


def t_a_minor_finding_does_not_block_ready() -> None:
    """Nur `blocker` und `major` halten auf. Sonst waere jede Randnotiz eine
    Sperre, und niemand schriebe mehr Randnotizen auf."""
    led, vertrag = _welt()
    mid, gruen = _bis_review(led, vertrag, gate_ok=True)
    for schluessel in ("gate", "lesbar"):
        led.set_criterion(mid, schluessel, state=S.CRIT_PROVEN, evidence_ref=gruen)
    led.open_finding(mid, severity="minor", title="Kosmetik")
    led.open_finding(mid, severity="info", title="Beobachtung")
    M.transition(led, mid, S.READY, gate_evidence_id=gruen, lead_verdict="READY")
    require_equal(led.milestone(mid).state, S.READY,
                  "eine Randnotiz hat READY verhindert")


def t_a_criterion_cannot_be_proven_without_evidence() -> None:
    """Amendment 9, als Schreibsperre statt als Bitte."""
    led, vertrag = _welt()
    exc = require_raises(S.LedgerError, led.set_criterion,
                         vertrag.milestone_id, "gate",
                         state=S.CRIT_PROVEN, evidence_ref="")
    require_equal(exc.reason, "proven_without_evidence", "falscher Grund")
    require_equal(led.acceptance_counts(vertrag.milestone_id), (0, 2),
                  "der Zaehler hat sich trotzdem bewegt")


# ------------------------------------------------------------- Evidence-Bindung
def t_test_evidence_from_another_commit_cannot_carry_a_review() -> None:
    """Evidence eines anderen Commits beschreibt einen anderen Baum."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    led.set_fields(mid, last_commit="c2")
    M.transition(led, mid, S.TESTING)
    alt = led.record_evidence(mid, kind="test_report", commit="c1",
                              env_fingerprint="fp1", ok=True, summary="alt")
    exc = require_raises(M.TransitionRefused, M.transition, led, mid,
                         S.REVIEWING, gate_evidence_id=alt)
    require_equal(exc.reason, "stale_test_evidence", "falscher Grund")

    passend = led.record_evidence(mid, kind="test_report", commit="c2",
                                  env_fingerprint="fp1", ok=True, summary="neu")
    M.transition(led, mid, S.REVIEWING, gate_evidence_id=passend)
    require_equal(led.milestone(mid).state, S.REVIEWING, "passende Evidence trug nicht")


def t_evidence_is_reusable_only_in_the_same_environment() -> None:
    """Gleicher Commit reicht NICHT — die Umgebung gehoert dazu.

    Genau diese Groesse hat beim Offsite-Release 14 Zusicherungen rot gemacht,
    die wie ein Codefehler aussahen und keiner waren.
    """
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    led.record_evidence(mid, kind="test_report", commit="c1",
                        env_fingerprint="mit-extras", ok=True, summary="gruen")
    require(led.fresh_evidence(mid, kind="test_report", commit="c1",
                               env_fingerprint="mit-extras") is not None,
            "gleiche Umgebung wurde nicht wiedergefunden")
    require(led.fresh_evidence(mid, kind="test_report", commit="c1",
                               env_fingerprint="ohne-extras") is None,
            "Evidence aus einer ANDEREN Umgebung galt als frisch")
    require(led.fresh_evidence(mid, kind="test_report", commit="c2",
                               env_fingerprint="mit-extras") is None,
            "Evidence eines ANDEREN Commits galt als frisch")


def t_review_without_any_test_evidence_is_refused() -> None:
    """Fehlende Messung ist ein Nein, kein Zweifelsfall."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    led.set_fields(mid, last_commit="c1")
    M.transition(led, mid, S.TESTING)
    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.REVIEWING)
    require_equal(exc.reason, "no_test_evidence", "falscher Grund")


# ------------------------------------------------------------------- die Kanten
def t_the_edge_table_is_closed() -> None:
    """Was nicht in der Tabelle steht, gibt es nicht."""
    verboten = ((S.PLANNING, S.READY), (S.PLANNING, S.TESTING),
                (S.TESTING, S.READY), (S.BUILDING, S.REVIEWING),
                (S.TESTING, S.FIXING), (S.FIXING, S.READY))
    for frm, to in verboten:
        require(not M.allowed(frm, to), f"{frm}->{to} war erlaubt")
    erlaubt = ((S.PLANNING, S.BUILDING), (S.BUILDING, S.TESTING),
               (S.TESTING, S.REVIEWING), (S.REVIEWING, S.FIXING),
               (S.FIXING, S.TESTING), (S.REVIEWING, S.READY))
    for frm, to in erlaubt:
        require(M.allowed(frm, to), f"{frm}->{to} fehlt in der Tabelle")


def t_a_red_test_is_never_a_human_boundary() -> None:
    """„Normale Bugs, rote Tests ... sind KEIN HUMAN_REQUIRED."

    Das ist hier keine Regel, an die man sich haelt, sondern eine fehlende
    Kante — und zusaetzlich eine ausdrueckliche Absage.
    """
    require(not M.allowed(S.TESTING, S.HUMAN_REQUIRED),
            "aus TESTING fuehrt eine Kante zum Menschen")

    # Und der echte Versuch, aus TESTING heraus: auch mit offener Grenze.
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    led.set_fields(mid, last_commit="c1")
    M.transition(led, mid, S.TESTING)
    led.open_boundary(mid, category="DECISION_REQUIRED", kind="product_decision",
                      question="Soll ich den Test lockern?")
    exc = require_raises(M.TransitionRefused, M.transition, led, mid,
                         S.HUMAN_REQUIRED)
    require_equal(exc.reason, "edge_not_allowed",
                  "ein rotes Gate kam zum Menschen durch")
    require_equal(led.milestone(mid).state, S.TESTING,
                  "der Milestone hat sich trotzdem bewegt")


def t_human_required_needs_an_actual_question() -> None:
    """Ein Wartezustand ohne Frage laesst den Menschen ratlos zurueck."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    exc = require_raises(M.TransitionRefused, M.transition, led, mid,
                         S.HUMAN_REQUIRED)
    require_equal(exc.reason, "no_open_boundary", "falscher Grund")

    M.park(led, mid, category="DECISION_REQUIRED", kind="product_decision",
           question="Welche der beiden Formen willst du?")
    require_equal(led.milestone(mid).state, S.HUMAN_REQUIRED, "park fuehrte nicht")
    require_equal(led.milestone(mid).state_before_park, S.PLANNING,
                  "der Vorzustand wurde nicht gemerkt")


def t_a_parked_milestone_returns_exactly_where_it_came_from() -> None:
    """Resume heisst „genau dort weiter", nicht „irgendwo weiter"."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    bid = M.park(led, mid, category="AUTHORITY_REQUIRED", kind="mfa_code",
                 question="Bitte den Code aus der App.")

    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.TESTING)
    require_equal(exc.reason, "boundary_still_open",
                  "eine offene Grenze liess den Lauf weiter")

    led.resolve_boundary(bid, "123456")
    exc = require_raises(M.TransitionRefused, M.transition, led, mid, S.REVIEWING)
    require_equal(exc.reason, "unpark_target_mismatch",
                  "der Lauf durfte woandershin zurueck")

    M.transition(led, mid, S.BUILDING)
    require_equal(led.milestone(mid).state, S.BUILDING, "Resume traf nicht")
    require_equal(led.milestone(mid).state_before_park, "",
                  "der gemerkte Vorzustand wurde nicht geloescht")


def t_blocked_needs_a_named_reason_and_can_wake_itself() -> None:
    """Ein Grund ohne Namen ist eine Ausrede — und Quota ist kein Mensch."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    exc = require_raises(M.TransitionRefused, M.transition, led, mid,
                         S.BLOCKED, block_reason="halt-mal")
    require_equal(exc.reason, "unknown_block_reason", "falscher Grund")

    M.transition(led, mid, S.BLOCKED, block_reason="capacity", resume_at=123.0)
    zustand = led.milestone(mid)
    require_equal(zustand.block_reason, "capacity", "der Grund fehlt")
    require_equal(zustand.resume_at, 123.0, "der Weckzeitpunkt fehlt")

    M.transition(led, mid, S.BUILDING)
    zustand = led.milestone(mid)
    require_equal(zustand.block_reason, "", "der Grund blieb nach dem Aufwachen stehen")
    require_equal(zustand.resume_at, 0.0, "der Weckzeitpunkt blieb stehen")


def t_a_terminal_milestone_stays_terminal() -> None:
    led, vertrag = _welt()
    mid, gruen = _bis_review(led, vertrag, gate_ok=True)
    for k in ("gate", "lesbar"):
        led.set_criterion(mid, k, state=S.CRIT_PROVEN, evidence_ref=gruen)
    M.transition(led, mid, S.READY, gate_evidence_id=gruen, lead_verdict="READY")
    for ziel in (S.BUILDING, S.FIXING, S.STOPPED, S.PLANNING):
        exc = require_raises(M.TransitionRefused, M.transition, led, mid, ziel)
        require_equal(exc.reason, "terminal_state", f"{ziel} war erreichbar")


# --------------------------------------------------------------- Ledger-Hygiene
def t_the_ledger_refuses_unknown_vocabulary() -> None:
    """Geschlossene Woerterbuecher. Ein unbekanntes Wort ist ein Tippfehler
    oder ein neues Konzept — beides gehoert bemerkt, nicht gespeichert."""
    led, vertrag = _welt()
    mid = vertrag.milestone_id

    exc = require_raises(S.LedgerError, led.record_event, mid, "geplauder")
    require_equal(exc.reason, "unknown_event_kind", "Ereignisart nicht geprueft")

    exc = require_raises(S.LedgerError, led.open_finding, mid,
                         severity="katastrophal", title="x")
    require_equal(exc.reason, "unknown_severity", "Schwere nicht geprueft")

    exc = require_raises(S.LedgerError, led.start_phase, mid,
                         kind="herumprobieren")
    require_equal(exc.reason, "unknown_phase_kind", "Phasenart nicht geprueft")

    exc = require_raises(S.LedgerError, led.set_fields, mid, freiheit="viel")
    require_equal(exc.reason, "unknown_field", "ein erfundenes Feld ging durch")


def t_the_ledger_survives_a_restart_with_its_truth() -> None:
    """Nach `kill -9` gibt es keinen Prozess mehr, aber die Wahrheit bleibt."""
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    vertrag = C.parse(BASIS)
    led.create_milestone(vertrag)
    mid = vertrag.milestone_id
    M.transition(led, mid, S.BUILDING)
    led.set_fields(mid, last_commit="c9", builder="codex")
    beleg = led.record_evidence(mid, kind="test_report", commit="c9",
                                env_fingerprint="fp", ok=True, summary="gruen")
    fid = led.open_finding(mid, severity="major", title="offen geblieben")
    led.close()

    wieder = S.AutopilotLedger(pfad)          # ein frischer Prozess
    zustand = wieder.milestone(mid)
    require_equal(zustand.state, S.BUILDING, "der Zustand ging verloren")
    require_equal(zustand.last_commit, "c9", "der Commit ging verloren")
    require_equal(zustand.contract_hash, vertrag.digest(), "der Contract-Hash driftete")
    require(wieder.evidence(beleg) is not None, "die Evidence ging verloren")
    require_equal([f.finding_id for f in wieder.findings(mid)], [fid],
                  "das offene Finding ging verloren")


def t_usage_never_invents_a_token_count() -> None:
    """Wenn der Anbieter nichts sagt, sagt das Ledger auch nichts.

    Gezaehlt wird trotzdem — aber als „Eintrag ohne Tokenangabe", nicht als
    Null. Eine Null waere eine Behauptung ueber den Verbrauch.
    """
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    led.record_usage(mid, role=S.ROLE_LEAD, provider="broker", calls=1,
                     wall_seconds=2.5, provider_tokens=1200)
    led.record_usage(mid, role=S.ROLE_BUILDER, provider="codex", calls=1,
                     wall_seconds=90.0, provider_tokens=None)
    zusammen = led.usage_summary(mid)
    require_equal(zusammen[S.ROLE_LEAD]["provider_tokens"], 1200,
                  "die gemeldeten Tokens fehlen")
    require_equal(zusammen[S.ROLE_BUILDER]["provider_tokens"], 0,
                  "eine fehlende Angabe wurde zu einer Zahl erfunden")
    require_equal(zusammen[S.ROLE_BUILDER]["eintraege_ohne_tokenangabe"], 1,
                  "die fehlende Angabe wurde nicht als solche gefuehrt")


def t_capacity_is_kept_per_role() -> None:
    """Amendment 8: ein Claude-Limit darf keinen Codex-Build blockieren.

    Das faengt damit an, dass die Rollen ueberhaupt getrennt gespeichert
    werden — eine gemeinsame Spalte koennte den Unterschied nicht ausdruecken.
    """
    led, vertrag = _welt()
    mid = vertrag.milestone_id
    led.set_capacity(mid, role=S.ROLE_LEAD, provider="broker",
                     state="AVAILABLE", signals={"authenticated": True})
    led.set_capacity(mid, role=S.ROLE_BUILDER, provider="codex",
                     state="EXHAUSTED", signals={"last_quota_hit_at": 1.0})
    led.set_capacity(mid, role=S.ROLE_ADVISOR, provider="claude",
                     state="UNKNOWN", signals={})
    lage = led.capacity(mid)
    require_equal(lage[S.ROLE_LEAD]["state"], "AVAILABLE", "TL-Lage falsch")
    require_equal(lage[S.ROLE_BUILDER]["state"], "EXHAUSTED", "Builder-Lage falsch")
    require_equal(lage[S.ROLE_ADVISOR]["state"], "UNKNOWN",
                  "UNKNOWN wurde zu etwas anderem gemacht")
    exc = require_raises(S.LedgerError, led.set_capacity, mid, role="hellseher",
                         provider="x", state="AVAILABLE", signals={})
    require_equal(exc.reason, "unknown_role", "eine erfundene Rolle wurde gespeichert")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
