"""Von der Luecke zur Fortsetzung — die Kette, die vorher einen Menschen brauchte.

Bis hierher endete eine erkannte Faehigkeitsluecke als Satz im Modellkontext:
`Dispatcher._with_way_forward` legte die `Resolution` nach
`payload["weiterweg"]`, und danach musste ein Mensch den Entwicklungsauftrag
von Hand anlegen. Gemessen: es gab im ganzen Repository keinen Pfad von einer
`Resolution` zu einem Entwicklungsauftrag — `SolutionPath` kam ausserhalb von
`resolver/` null mal vor, `resolver` in `autopilot/` null mal.

Diese Suite prueft die Naht als VERHALTEN, nicht als Vorhandensein:

* der VORHANDENE Gap Resolver wird gefragt, nicht ein zweiter gebaut;
* seine Schranken gelten unveraendert — Freigabebedarf, Ablehnung und
  ungewisser Ausgang loesen keine Entwicklung aus;
* der Auftrag des Nutzers ueberlebt die Entwicklung und wird fortgesetzt;
* ein gelungener Bau ist KEINE Bereitstellung.

Isolation: `SOLVIO_STATE_DIR`, `SOLVIO_AGENT_RUNS_DB` und
`SOLVIO_AUTOPILOT_DB` zeigen in ein frisches Temp-Verzeichnis — gesetzt NACH
dem Import der Schwestersuite, weil die beim Import ihre eigenen setzt und
sonst gewaenne. Kein Anbieter, kein Netz, keine Aussenhandlung.
"""
from __future__ import annotations

import atexit
import json
import os
import time
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

_SANDBOX = tempfile.mkdtemp(prefix="solvio-gap-suite-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from _guard import enforce_assertions, require, require_equal  # noqa: E402

# Ohne diesen Aufruf laeuft die Suite unter `-O` mit entfernten `assert`s
# durch und meldet gruen. Eine Zusicherung in
# `test_p1a_5_blocker_remediation.py` haelt fest, dass JEDE Suite ihn tut —
# meine erste Fassung tat es nicht, und das Gate hat es gefunden.
enforce_assertions()

# ZUERST die Schwestersuite, DANN die eigene Umleitung — und zwar aus einem
# gemessenen Grund: `test_objective_execution_v1a` setzt beim Import selbst
# `SOLVIO_STATE_DIR` und `SOLVIO_AGENT_RUNS_DB` auf SEINEN Sandkasten. Stuende
# die Umleitung oben, gewaenne der Import, und diese Suite haette eine
# Isolation behauptet, die sie nicht kontrolliert. Die Zusicherung unten hat
# genau das gefunden.
#
# Die Pfade werden zur AUFRUFZEIT aus der Umgebung gelesen (`resolve_path`),
# also wirkt die Umleitung auch nach dem Import.
import test_objective_execution_v1a as V1A                     # noqa: E402

os.environ["SOLVIO_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_SANDBOX, "agent_runs.sqlite3")
os.environ["SOLVIO_AUTOPILOT_DB"] = os.path.join(_SANDBOX, "autopilot.sqlite3")
from solvio.agent_runtime import development as DEV            # noqa: E402
from solvio.agent_runtime import store as S                    # noqa: E402
from solvio.autopilot import store as A                        # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome as OUT  # noqa: E402
from solvio.capabilities.envelope import CapabilityResult       # noqa: E402
from solvio.resolver.inventory import CapabilityInventory       # noqa: E402
from solvio.resolver.resolver import GapResolver                # noqa: E402

FEHLT = "calendar_create_event"
ZIEL = "Trag mir den Zahnarzttermin am Dienstag in den Kalender ein."


class Router(V1A.Router):
    """Kennt die Faehigkeit ERST, nachdem sie bereitgestellt wurde.

    Das ist die kontrolliert fehlende Faehigkeit: `spec()` liefert `None`, also
    ist sie fuer `DEV.is_available` nicht da — genau die Frage, die der Lauf
    nach der Entwicklung stellt.
    """

    def __init__(self, ergebnis=None) -> None:
        super().__init__(specs={})
        self.bereitgestellt = False
        self._ergebnis = ergebnis

    def names(self):
        return [FEHLT] if self.bereitgestellt else []

    def spec(self, name):
        if name == FEHLT and self.bereitgestellt:
            return V1A.Spec({"type": "object", "properties": {}})
        return None

    async def execute(self, name, arguments=None, **kw):
        self.calls.append({"name": name, "arguments": dict(arguments or {})})
        if self._ergebnis is not None and not self.bereitgestellt:
            return self._ergebnis
        if name == FEHLT and not self.bereitgestellt:
            return CapabilityResult(OUT.CAPABILITY_FAILED, f"c{len(self.calls)}",
                                    name, reason="unknown_capability",
                                    human_message="Die kenne ich nicht.")
        return CapabilityResult(OUT.SUCCESS, f"c{len(self.calls)}", name,
                                human_message="erledigt")


def _buch() -> A.AutopilotLedger:
    return A.AutopilotLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                          "autopilot.sqlite3"))


def _aufbau(*, ergebnis=None, plan=None):
    """Ein Lauf mit ECHTEM Resolver und eigenem Autopilot-Buch."""
    router = Router(ergebnis=ergebnis)
    buch = _buch()
    orch = V1A._orch(plans=[plan or [V1A._cap(FEHLT, titel="Zahnarzt")]],
                     router=router)
    orch.gap_resolver = GapResolver(CapabilityInventory(router))
    orch.memory_owner_principal = "local-owner"
    orch.development = buch
    return orch, router, buch


def _bis_zum_parken(orch, run_id, ticks=8):
    for _ in range(ticks):
        V1A._run(orch.tick())
        zustand = orch.ledger.get_run(run_id).state
        if zustand == S.WAITING_CAPABILITY or zustand in S.TERMINAL_STATES:
            break
    return orch.ledger.get_run(run_id)


# =====================================================================
# Die Kette
# =====================================================================

def t_a_real_gap_commissions_development_and_parks_the_order():
    """**Der Kern.** Die Luecke wird zum Auftrag, und der Nutzerauftrag bleibt.

    Vorher endete genau hier die Maschine und ein Mensch uebernahm.
    """
    orch, router, buch = _aufbau()
    task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)

    require_equal(ende.state, S.WAITING_CAPABILITY,
                  f"der Lauf parkt nicht: {ende.state} / {ende.result_summary[:80]}")
    require(ende.development_ref, "die Zuordnung fehlt im Buch")
    milestone = buch.milestone(ende.development_ref)
    require_equal(milestone.state, A.PLANNING)
    # Die Rueckrichtung: der Vertrag nennt den Lauf, der ihn ausgeloest hat.
    require(run.run_id in milestone.contract_json,
            "der Entwicklungsvertrag nennt den Lauf nicht")
    require(task.task_id in milestone.contract_json,
            "der Entwicklungsvertrag nennt die Aufgabe nicht")
    # Und den Grund, warum die Faehigkeit gebraucht wird.
    require(ZIEL[:40] in milestone.contract_json,
            "der Auftrag des Nutzers steht nicht im Entwicklungsvertrag")


def t_the_same_gap_never_commissions_twice():
    """Wiederholte Ereignisse und Neustarts erzeugen keinen zweiten Auftrag.

    Die Einmaligkeit kommt aus dem Primaerschluessel des Autopilot-Buchs, nicht
    aus einer Pruefung davor — ein `SELECT` davor waere ein Wettlauf.
    """
    orch, router, buch = _aufbau()
    _task, run = V1A._create(orch, objective=ZIEL)
    _bis_zum_parken(orch, run.run_id)
    require_equal(len(buch.milestones()), 1, "kein Auftrag angelegt")

    for _ in range(3):
        V1A._run(orch.tick())
    require_equal(len(buch.milestones()), 1,
                  f"{len(buch.milestones())} Auftraege nach mehreren Takten")

    # Und nach einem Prozessverlust: derselbe Lauf, frischer Orchestrator.
    zweiter = V1A._orch(plans=[[V1A._cap(FEHLT)]], router=router,
                        ledger=orch.ledger)
    zweiter.gap_resolver = orch.gap_resolver
    zweiter.development = buch
    V1A._run(zweiter.reconcile())
    for _ in range(3):
        V1A._run(zweiter.tick())
    require_equal(len(buch.milestones()), 1,
                  "ein Neustart hat einen zweiten Auftrag erzeugt")


def t_a_restart_leaves_the_waiting_order_alone():
    """Der Neustart wirft den Auftrag nicht weg.

    Die Entwicklung laeuft im Autopilot-Buch weiter, auch wenn der Core
    endet — deshalb ist `WAITING_CAPABILITY` ein PARKENDER Zustand.
    """
    orch, router, buch = _aufbau()
    _task, run = V1A._create(orch, objective=ZIEL)
    _bis_zum_parken(orch, run.run_id)

    frisch = V1A._orch(plans=[[V1A._cap(FEHLT)]], router=router, ledger=orch.ledger)
    frisch.gap_resolver = orch.gap_resolver
    frisch.development = buch
    bericht = V1A._run(frisch.reconcile())
    require(run.run_id not in bericht.get("interrupted", []),
            "der wartende Lauf wurde beim Neustart unterbrochen")
    require_equal(frisch.ledger.get_run(run.run_id).state, S.WAITING_CAPABILITY)


def t_a_finished_build_is_not_provision():
    """**Ein gelungener Bau bedeutet keine Bereitstellung.**

    Der Autopilot endet bei Commit und Ref; eine Faehigkeit entsteht erst beim
    Prozessstart des Cores. `READY` ohne aufrufbare Faehigkeit weckt den Lauf
    deshalb NICHT — es wird eine Owner-Grenze daraus.
    """
    orch, router, buch = _aufbau()
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    for zustand in (A.BUILDING, A.TESTING, A.REVIEWING, A.READY):
        buch.set_state(ende.development_ref, zustand)

    V1A._run(orch.tick())
    danach = orch.ledger.get_run(run.run_id)
    require_equal(danach.state, S.WAITING_USER,
                  f"der Lauf lief ohne Bereitstellung weiter: {danach.state}")
    require(danach.boundary, "es entstand keine Owner-Grenze")
    require_equal([c["name"] for c in router.calls], [FEHLT],
                  "die Faehigkeit wurde ohne Bereitstellung gerufen")


def t_after_provision_the_original_order_continues():
    """Und nach der Bereitstellung wird DERSELBE Auftrag fortgesetzt.

    Nicht ein neuer Lauf, nicht eine neue Aufgabe: derselbe Lauf nimmt denselben
    Plan an derselben Stelle wieder auf.
    """
    orch, router, buch = _aufbau()
    task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    for zustand in (A.BUILDING, A.TESTING, A.REVIEWING, A.READY):
        buch.set_state(ende.development_ref, zustand)
    V1A._run(orch.tick())                       # -> Owner-Grenze

    router.bereitgestellt = True                # Bereitstellung im Testsystem
    require(V1A._run(orch.resume(run.run_id)), "die Wiederaufnahme wurde verweigert")
    for _ in range(6):
        V1A._run(orch.tick())
        if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
            break

    gerufen = [c["name"] for c in router.calls]
    require_equal(gerufen, [FEHLT, FEHLT],
                  f"die Faehigkeit wurde nicht erneut gerufen: {gerufen}")
    schritte = [(s.seq, s.attempt, s.state)
                for s in orch.ledger.steps_for_run(run.run_id)
                if s.kind == "capability"]
    require_equal(schritte, [(1, 1, "failed"), (1, 2, "succeeded")],
                  f"der Schritt wurde nicht sauber wiederholt: {schritte}")
    # Es ist derselbe Auftrag geblieben — keine zweite Aufgabe entstanden.
    require_equal(orch.ledger.get_run(run.run_id).task_id, task.task_id)


# =====================================================================
# Was KEINE technische Luecke ist
# =====================================================================

def t_neither_approval_nor_refusal_nor_unknown_effect_commissions_anything():
    """**Die drei Nicht-Luecken.** Keine davon darf Ersatzentwicklung ausloesen.

    Gezaehlt wird das Autopilot-Buch, nicht ein Docstring. Die Schranken
    stammen aus dem vorhandenen Resolver — hier steht nur, dass sie im
    Agentenpfad genauso gelten wie im Werkzeugpfad.
    """
    faelle = [
        ("Freigabepflicht", CapabilityResult(
            OUT.APPROVAL_REQUIRED, "c1", FEHLT, reason="awaiting_user_approval",
            data={"request_id": "ap-1"}, human_message="brauche deine Freigabe")),
        ("Ablehnung", CapabilityResult(
            OUT.REJECTED_BY_POLICY, "c1", FEHLT, reason="untrusted_origin",
            human_message="nicht erlaubt")),
        ("ungewisser Ausgang", CapabilityResult(
            OUT.RECOVERY_REQUIRED, "c1", FEHLT, reason="claimed_unknown",
            human_message="Ausgang ungewiss")),
    ]
    for name, ergebnis in faelle:
        orch, _router, buch = _aufbau(ergebnis=ergebnis)
        _task, run = V1A._create(orch, objective=ZIEL)
        for _ in range(6):
            V1A._run(orch.tick())
            if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
                break
        require_equal(len(buch.milestones()), 0,
                      f"{name}: eine Entwicklung wurde beauftragt")
        require_equal(orch.ledger.get_run(run.run_id).development_ref, "",
                      f"{name}: eine Zuordnung wurde geschrieben")


def t_a_transient_disturbance_is_no_reason_to_design_code():
    """**Die Schranke, die meine ersten drei Gegenfaelle nie erreichten.**

    Freigabepflicht, Ablehnung und ungewisser Ausgang werden schon von
    FRUEHEREN Zweigen des Settlements abgefangen — sie kommen bei
    `may_propose_capability` gar nicht an. Gemessen: mit entfernter Schranke
    blieb die Suite gruen. Diese Zusicherung faehrt deshalb einen Fall, der
    wirklich dort ankommt.

    Ein nicht erreichbarer Ausfuehrer ist eine STOERUNG: sie geht vorueber,
    und neue Faehigkeit hilft nicht. Ein Serverausfall ist kein Grund, Code zu
    entwerfen.
    """
    voruebergehend = [
        ("Ausfuehrer weg", CapabilityResult(
            OUT.CAPABILITY_FAILED, "c1", FEHLT, reason="executor_unavailable",
            human_message="gerade nicht erreichbar")),
        ("Kontingent alle", CapabilityResult(
            OUT.CAPABILITY_FAILED, "c1", FEHLT, reason="quota_exceeded",
            human_message="Kontingent erschoepft")),
        ("Zugangsdaten fehlen", CapabilityResult(
            OUT.CAPABILITY_FAILED, "c1", FEHLT, reason="credentials_missing",
            human_message="keine Zugangsdaten")),
    ]
    for name, ergebnis in voruebergehend:
        orch, _router, buch = _aufbau(ergebnis=ergebnis)
        _task, run = V1A._create(orch, objective=ZIEL)
        for _ in range(6):
            V1A._run(orch.tick())
            if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
                break
        require_equal(len(buch.milestones()), 0,
                      f"{name}: eine Entwicklung wurde beauftragt")
        require(orch.ledger.get_run(run.run_id).state != S.WAITING_CAPABILITY,
                f"{name}: der Lauf wartet auf eine Entwicklung")


def _alternative_lage(*, granted=True):
    """Echtes Register/Inventar/Resolver/Grant; nur Plan und Leser sind lokal.

    Der fehlgeschlagene Schritt ist der Eingang dieser Naht. Der anfaengliche
    Grant wird am internen Core-Vertrag gesetzt; keine Browseranmeldung oder
    neue Freigabe wird durch diese Runtime-Probe behauptet.
    """
    from types import SimpleNamespace
    from solvio.capabilities.portal import PortalCapabilities, SPECS, _LocalPortalList
    from solvio.portal.vault import PortalVault
    from solvio.capabilities.router import CapabilityRouter
    from solvio.agent_runtime.task_authority import CapabilityGrant, VerifiedTaskReceipt

    name = "portal_list"
    missing = "missing_portal_reader"
    objective = "Zeige die eingerichteten Portale und oeffne die Zugaenge."
    calls = []
    # N7 requires an actual reviewed local service, not a READ_ONLY label on
    # a synthetic calendar handler. Count the real Router dispatch; the empty
    # temporary vault cannot read a keychain or contact the PortalClient.
    router = CapabilityRouter(recorder=lambda event:
        calls.append({}) if event.capability == name and event.phase == "started" else None)
    vault = PortalVault(base_dir=tempfile.mkdtemp(dir=_SANDBOX))
    portals = PortalCapabilities(client=None, vault=vault)
    router.register(SPECS[name], _LocalPortalList(portals, vault, portals.list_portals))

    class Planner(V1A.Planner):
        def __init__(self):
            super().__init__([[V1A._cap(name)]])
            self.inputs = []

        async def plan(self, **kwargs):
            self.inputs.append({"context": kwargs["context"],
                                "known": set(kwargs["known_capabilities"]),
                                "run_id": kwargs["run_id"]})
            return await super().plan(**kwargs)

    class Resolver(GapResolver):
        async def for_failed_tool(self, **kwargs):
            self.last = await super().for_failed_tool(**kwargs)
            return self.last

    planner = Planner()
    orch = V1A._orch(router=router, planner=planner)
    orch.gap_resolver = Resolver(CapabilityInventory(router, probes={name: lambda: True}))
    orch.development = _buch()
    task, run = V1A._create(orch, objective=objective)
    grant = orch.task_authority.issue(task.task_id, run.run_id,
        receipt=VerifiedTaskReceipt("app_session", "test-gap:" + run.run_id, "local-owner"),
        capabilities=(CapabilityGrant(name, 1),) if granted else ())
    orch.ledger.transition(run.run_id, S.PLANNING)
    orch.ledger.transition(run.run_id, S.RUNNING)
    run = orch.ledger.get_run(run.run_id)
    step = orch.ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                   attempt=1, capability=missing)
    orch.ledger.update_step(step.step_id, state="running", started=True)
    outcome = V1A.ST.StepOutcome(state="failed", failure_category="capability_failed",
        outcome_reason="capability_failed:unknown_capability", human_message="Der direkte Weg fehlt.")
    return SimpleNamespace(orch=orch, router=router, planner=planner, task=task, run=run,
        context=orch._contexts[run.run_id], step=step, planned=V1A._cap(missing),
        outcome=outcome, grant=grant, calls=calls, alternative=name)


def t_an_existing_granted_solution_informs_replanning_without_commission_or_dispatch():
    from solvio.resolver.states import ResolverState
    for with_development in (True, False):
        w = _alternative_lage()
        development = w.orch.development
        if not with_development:
            w.orch.development = None
        V1A._run(w.orch._settle_capability(w.run, w.context, w.step, w.outcome, w.planned))
        resolution = getattr(w.orch.gap_resolver, "last", None)
        require(resolution is not None, "Wiederverwendung darf kein Entwicklungsbuch voraussetzen")
        require_equal(resolution.state, ResolverState.SOLUTION_FOUND)
        require_equal(resolution.proposal, None)
        require_equal(development.milestones(), [])
        require_equal(w.calls, [], "der Resolverhinweis darf nicht selbst dispatchen")
        require_equal(len(w.planner.inputs), 1)
        require(w.alternative in w.planner.inputs[0]["context"], "der vorhandene Weg fehlt im Replan")
        require_equal(w.planner.inputs[0]["known"], {w.alternative})
        require_equal(w.planner.inputs[0]["run_id"], w.run.run_id)
        require_equal(w.orch.ledger.get_run(w.run.run_id).plan_revision, 1)
        require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)

        # Erst der naechste normale Laufzeitschritt verwendet die Alternative,
        # durch den echten Router und denselben bereits bestehenden Grant.
        V1A._run(w.orch._advance(w.orch.ledger.get_run(w.run.run_id)))
        require_equal(w.calls, [{}])
        require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)
        require_equal(len(w.orch.ledger.recent_runs()), 1)


def t_an_existing_but_ungranted_solution_is_neither_recommended_nor_built():
    from solvio.resolver.states import ResolverState
    w = _alternative_lage(granted=False)
    V1A._run(w.orch._settle_capability(w.run, w.context, w.step, w.outcome, w.planned))
    require_equal(w.orch.gap_resolver.last.state, ResolverState.SOLUTION_FOUND)
    require_equal(w.orch.development.milestones(), [])
    require_equal(w.calls, [])
    require_equal(len(w.planner.inputs), 1, "der bisherige Fehler-Replan bleibt erhalten")
    require(w.alternative not in w.planner.inputs[0]["context"], "Inventar wurde zur Befugnis")
    require_equal(w.planner.inputs[0]["known"], set())
    require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)
    # Die lokale Planerattrappe nennt die Alternative trotzdem. Auch dann
    # bleibt der echte Router an der unveraenderten Auftragsgrenze stehen.
    V1A._run(w.orch._advance(w.orch.ledger.get_run(w.run.run_id)))
    require_equal(w.calls, [])
    require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)


def t_a_found_solution_uses_the_existing_replan_budget():
    w = _alternative_lage()
    limit = w.context.ledger.budget.max_plan_revisions
    w.context.ledger.plan_revisions = limit
    w.orch.ledger.set_run_fields(w.run.run_id, plan_revision=limit)
    V1A._run(w.orch._settle_capability(w.orch.ledger.get_run(w.run.run_id),
                                      w.context, w.step, w.outcome, w.planned))
    current = w.orch.ledger.get_run(w.run.run_id)
    require_equal(current.state, S.FAILED)
    require_equal(current.failure_category, "budget_exhausted")
    require_equal(w.planner.inputs, [])
    require_equal(w.orch.development.milestones(), [])
    require_equal(w.calls, [])
    require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)


def t_only_proposal_ready_with_a_real_proposal_can_commission():
    from dataclasses import replace
    from unittest.mock import AsyncMock, patch
    from solvio.resolver.states import ResolverState
    from solvio.resolver.proposal import CapabilityProposal
    orch, _router, _buch0 = _aufbau()
    resolution = V1A._run(orch.gap_resolver.for_failed_tool(
        error="capability_failed:unknown_capability", tool=FEHLT, goal=ZIEL,
        trust=V1A.ST.agent_trust("test-task", "2026-09-10")))
    require_equal(resolution.state, ResolverState.PROPOSAL_READY)
    require(isinstance(resolution.proposal, CapabilityProposal))
    cases = [(state, resolution.proposal) for state in ResolverState
             if state is not ResolverState.PROPOSAL_READY]
    cases += [(ResolverState.PROPOSAL_READY, missing) for missing in (None, {}, object())]
    for state, proposal in cases:
        w = _alternative_lage()
        answer = replace(resolution, state=state, proposal=proposal, paths=[])
        with patch.object(w.orch.gap_resolver, "for_failed_tool", AsyncMock(return_value=answer)):
            handled = V1A._run(w.orch._commission_development(
                w.run, w.context, w.step, w.planned, w.outcome))
        require_equal(handled, False, f"{state}: als Entwicklung behandelt")
        require_equal(w.orch.development.milestones(), [], f"{state}: Entwicklungsauftrag")
        require_equal(w.planner.inputs, [])
        require_equal(w.calls, [])
        require_equal(w.orch.task_authority.for_run(w.run.run_id), w.grant)


def t_a_development_that_does_not_carry_ends_the_run_honestly():
    """Fehlgeschlagener Bau, rotes Gate, blockierte Entwicklung — kein ewiges Warten.

    `TERMINAL_STATES` des Autopiloten kennt nur `READY` und `STOPPED`;
    `BLOCKED` und `HUMAN_REQUIRED` sind formal nicht terminal, aber es arbeitet
    dort niemand mehr. Wer auf sie wartet, wartet fuer immer.
    """
    for zustand in (A.STOPPED, A.BLOCKED, A.HUMAN_REQUIRED):
        orch, _router, buch = _aufbau()
        _task, run = V1A._create(orch, objective=ZIEL)
        ende = _bis_zum_parken(orch, run.run_id)
        buch.set_state(ende.development_ref, zustand)
        for _ in range(4):
            V1A._run(orch.tick())
            if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
                break
        danach = orch.ledger.get_run(run.run_id)
        require(danach.state in S.TERMINAL_STATES,
                f"{zustand}: der Lauf wartet weiter ({danach.state})")
        require_equal(danach.failure_category, "capability_failed",
                      f"{zustand}: falscher Grund")


def t_a_cancelled_order_stops_waiting_for_development():
    """Bricht der Nutzer ab, wartet niemand mehr auf die Entwicklung."""
    orch, _router, buch = _aufbau()
    _task, run = V1A._create(orch, objective=ZIEL)
    _bis_zum_parken(orch, run.run_id)
    require(V1A._run(orch.cancel(run.run_id)), "der Lauf liess sich nicht abbrechen")
    require_equal(orch.ledger.get_run(run.run_id).state, S.CANCELLED)
    for _ in range(3):
        V1A._run(orch.tick())
    require_equal(orch.ledger.get_run(run.run_id).state, S.CANCELLED,
                  "ein abgebrochener Lauf wurde wieder aufgenommen")


def t_without_the_seam_the_runtime_works_exactly_as_before():
    """Fehlt Resolver oder Autopilot-Buch, laeuft alles wie vorher.

    Dieselbe Zusage wie beim Rechercheweg: die Naht ist fail-soft angehaengt.
    """
    for weglassen in ("gap_resolver", "development"):
        orch, _router, buch = _aufbau()
        setattr(orch, weglassen, None)
        _task, run = V1A._create(orch, objective=ZIEL)
        for _ in range(6):
            V1A._run(orch.tick())
            if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
                break
        require(orch.ledger.get_run(run.run_id).state != S.WAITING_CAPABILITY,
                f"ohne {weglassen} parkte der Lauf trotzdem")
        require_equal(len(buch.milestones()), 0,
                      f"ohne {weglassen} entstand ein Auftrag")


# =====================================================================
# Die Kennung und die Verfuegbarkeitsfrage
# =====================================================================

def t_the_milestone_id_is_deterministic_and_specific():
    """Dieselbe Luecke, dieselbe Kennung — eine andere Luecke, eine andere."""
    a = DEV.milestone_id_for(run_id="ar-1", capability="x", kind="capability_missing")
    require_equal(a, DEV.milestone_id_for(run_id="ar-1", capability="x",
                                          kind="capability_missing"),
                  "die Kennung ist nicht deterministisch")
    for feld, wert in (("run_id", "ar-2"), ("capability", "y"),
                       ("kind", "unsupported_variant")):
        anders = dict(run_id="ar-1", capability="x", kind="capability_missing")
        anders[feld] = wert
        require(DEV.milestone_id_for(**anders) != a,
                f"ein anderes {feld} ergibt dieselbe Kennung")
    import re
    require(re.match(r"^[a-z0-9][a-z0-9-]{2,63}$", a),
            f"die Kennung passt nicht ins Autopilot-Schema: {a}")


def t_availability_is_asked_of_the_router_not_of_the_autopilot():
    """Unwissenheit ist hier ein Nein."""
    router = Router()
    require(not DEV.is_available(router, FEHLT), "nicht bereitgestellt gilt als da")
    router.bereitgestellt = True
    require(DEV.is_available(router, FEHLT), "bereitgestellt gilt als fehlend")
    require(not DEV.is_available(None, FEHLT), "ohne Router gilt etwas als da")
    require(not DEV.is_available(router, ""), "ohne Namen gilt etwas als da")

    class Kaputt:
        def spec(self, name):
            raise RuntimeError("Buch unlesbar")

    require(not DEV.is_available(Kaputt(), FEHLT),
            "ein Lesefehler gilt als vorhanden")


def t_this_suite_touches_no_production_state():
    """Die Isolation wird zugesichert, nicht erinnert."""
    for name in ("SOLVIO_STATE_DIR", "SOLVIO_AGENT_RUNS_DB", "SOLVIO_AUTOPILOT_DB"):
        wert = os.path.realpath(os.path.expanduser(os.environ.get(name, "")))
        require(wert.startswith(os.path.realpath(_SANDBOX) + os.sep),
                f"{name} zeigt aus dem Sandkasten heraus: {wert}")
    produktiv = os.path.realpath(os.path.expanduser("~/.solvio"))
    for pfad in (S.resolve_path(), A.resolve_path()):
        echt = os.path.realpath(os.path.expanduser(pfad))
        require(not echt.startswith(produktiv + os.sep),
                f"ein Buch liegt im produktiven Bereich: {echt}")


# =====================================================================
# Das zweite Gehirn — lesend, informierend, nie autorisierend
# =====================================================================

class Gedaechtnis:
    """Der kanonische Abrufweg in der Form, die `MemoryService.search` liefert."""

    def __init__(self, treffer=(), faellt_aus=False) -> None:
        self.fragen: list[str] = []
        self._treffer = list(treffer)
        self._faellt_aus = faellt_aus

    async def search(self, query, *, top_k=5, max_chars=0):
        self.fragen.append(query)
        if self._faellt_aus:
            raise RuntimeError("Einbettungsmodell nicht ladbar")
        return self._treffer[:top_k]


class Treffer:
    def __init__(self, content, art="semantic", wann="2026-08-01T10:00:00Z"):
        self.content = content
        self.memory_type = art
        self.created_at = wann
        self.memory_id = "m-1"
        self.relevance = 1


def t_known_solutions_inform_the_development_order():
    """Was das Gedaechtnis weiss, steht im Auftrag — mit Herkunft und Datum.

    Und ausdruecklich als Information: der Vertrag sagt „keine Vorgabe".
    """
    weiss = Gedaechtnis([Treffer("Kalendereintraege laufen ueber die "
                                 "Google-Calendar-API mit dem Zweitzugang.")])
    orch, _router, buch = _aufbau()
    orch.knowledge = weiss
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)

    require(weiss.fragen, "das Gedaechtnis wurde nicht gefragt")
    require(FEHLT in weiss.fragen[0] and ZIEL[:20] in weiss.fragen[0],
            f"die Frage traegt nicht Luecke und Ziel: {weiss.fragen[0][:80]}")
    vertrag = buch.milestone(ende.development_ref).contract_json
    require("Google-Calendar-API" in vertrag,
            "das Bekannte steht nicht im Entwicklungsauftrag")
    require("keine Vorgabe" in vertrag,
            "das Bekannte ist nicht als Information gekennzeichnet")
    require("2026-08-01" in vertrag, "das Datum reist nicht mit")
    require("semantic" in vertrag, "die Herkunftsart reist nicht mit")


def t_a_silent_memory_never_blocks_a_development():
    """Ein Gedaechtnis, das schweigt oder ausfaellt, haelt nichts auf."""
    for name, weiss in (("stumm", Gedaechtnis([])),
                        ("ausgefallen", Gedaechtnis(faellt_aus=True)),
                        ("gar keins", None)):
        orch, _router, buch = _aufbau()
        orch.knowledge = weiss
        _task, run = V1A._create(orch, objective=ZIEL)
        ende = _bis_zum_parken(orch, run.run_id)
        require_equal(ende.state, S.WAITING_CAPABILITY,
                      f"{name}: die Entwicklung kam nicht zustande")
        require_equal(len(buch.milestones()), 1, f"{name}: kein Auftrag")


def t_the_runtime_has_no_write_path_into_memory():
    """**Die Grenze.** Es gibt keinen Schreibweg — nicht gefiltert, nicht verdrahtet.

    Eine erprobte Loesung wird ueber den vorhandenen Vorschlagsweg
    wiederauffindbar (`knowledge_proposal` legt ein Artefakt ab), und die
    Aufnahme ins Gedaechtnis bleibt eine Owner-Handlung mit turn-gebundenem
    Mandat. Automatik waere hier `user_direct` ohne Nutzer.
    """
    import ast
    quelle = open(os.path.join(os.path.dirname(DEV.__file__),
                               "development.py"), encoding="utf-8").read()
    gerufen = {n.func.attr for n in ast.walk(ast.parse(quelle))
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for verboten in ("remember", "store", "write", "add_record", "upsert"):
        require(verboten not in gerufen,
                f"die Naht ruft {verboten} — das waere ein Schreibweg")
    require("search" in gerufen, "der Leseweg fehlt")


# =====================================================================
# Der Auftrag startet den Entwickler — ohne menschlichen Vermittler
#
# Bis hierher legte SOLVIO den Entwicklungsauftrag an, und niemand fuhr ihn.
# Der einzige belegte Ausfuehrungsstart lag in
# `scripts/autopilot.py run --milestone …` — also wieder bei einem Menschen an
# einer Tastatur.
#
# **In dieser Sektion setzt kein Test einen Zustand.** Was der Auftrag
# durchlaeuft, durchlaeuft er, weil der VORHANDENE Treiber ihn faehrt. Der
# Builder ist kontrolliert (`B.SyntheticBuilder` aus dem Produktivcode), aber
# er wird ueber `Driver(adapters=…)` gerufen — die vorhandene Konstruktornaht,
# nicht daran vorbei.
# =====================================================================

import test_autopilot_driver as APD                             # noqa: E402
from solvio.autopilot import builders as B                      # noqa: E402
from solvio.autopilot import driver as D                        # noqa: E402

os.environ["SOLVIO_AUTOPILOT_LOCK"] = os.path.join(_SANDBOX, "autopilot.lock")


class _Lead(APD._Lead):
    """Ein Technical Lead, der seinen Beleg im LAUF findet statt vorher.

    `APD._Lead` nimmt eine feste Urteilsliste; ein `REVIEW_SUPPORTED`-Kriterium
    braucht aber eine Evidence-Referenz, die es beim Anlegen des Auftrags noch
    nicht gibt. Ein statisches Urteil laeuft deshalb ins Leere, und der Auftrag
    endet auf `HUMAN_REQUIRED` — gemessen beim ersten Abnahmelauf.

    Gestellt ist auch hier NUR der Modellaufruf: `LEAD.validate()` laeuft echt,
    und ein Urteil, das der Core nicht annehmen wuerde, wird auch hier nicht
    angenommen. Ein `READY` bei rotem Gate traegt nach wie vor nicht — das
    entscheidet der Treiber, nicht dieses Fixture.
    """

    def __init__(self) -> None:
        super().__init__([])

    async def judge(self, *, context, allowed_builders, open_finding_ids,
                    criterion_keys, evidence_ids, deterministic_keys,
                    tier="small"):
        belegbar = sorted(set(criterion_keys) - set(deterministic_keys))
        beleg = sorted(evidence_ids)[0] if evidence_ids else ""
        self.urteile = [{
            "verdict": "READY", "next_action": "ready",
            "rationale": "Gate gruen, Kriterien belegt",
            "proven": ([{"key": k, "evidence_ref": beleg} for k in belegbar]
                       if beleg else [])}]
        return await super().judge(
            context=context, allowed_builders=allowed_builders,
            open_finding_ids=open_finding_ids, criterion_keys=criterion_keys,
            evidence_ids=evidence_ids, deterministic_keys=deterministic_keys,
            tier=tier)


def _mit_treiber(*, gate=None, urteile=None):
    """Ein Lauf, dessen Entwicklungsauftrag WIRKLICH gefahren wird."""
    orch, router, buch = _aufbau()
    werk = APD._arbeitsbereich(gate=gate or APD.GATE_ROT)
    bauer = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    lead = _Lead() if urteile is None else APD._Lead(urteile)

    def fabrik(milestone_id=""):
        return D.Driver(buch, workspace=werk, adapters={"synthetic": bauer},
                        lead=lead, python=sys.executable)

    orch.driver_factory = fabrik
    return orch, router, buch, bauer, lead, werk


async def _drehen(orch, run_id, runden=12):
    """Takten und die gestarteten Treiber wirklich zu Ende laufen lassen."""
    for _ in range(runden):
        await orch.tick()
        for aufgabe in list(orch._drivers.values()):
            if not aufgabe.done():
                await aufgabe
        if orch.ledger.get_run(run_id).state in S.TERMINAL_STATES:
            break
    return orch.ledger.get_run(run_id)


def t_the_order_starts_the_developer_without_a_human():
    """**Der verlangte Beleg.**

    Kein CLI-Aufruf, kein kopierter Prompt, kein von Hand gesetzter Zustand:
    der Auftrag entsteht aus der Luecke, der vorhandene Treiber uebernimmt ihn,
    ruft den Builder und faehrt seine Pruefungen.
    """
    orch, _router, buch, bauer, lead, _werk = _mit_treiber()
    _task, run = V1A._create(orch, objective=ZIEL)
    V1A._run(_drehen(orch, run.run_id))

    kennung = orch.ledger.get_run(run.run_id).development_ref
    require(kennung, "es entstand kein Entwicklungsauftrag")
    stein = buch.milestone(kennung)

    require(bauer.calls >= 1, "der Builder wurde nie gerufen")
    require_equal(stein.state, A.READY,
                  f"der Auftrag kam nicht durch: {stein.state}")
    # Die Pruefungen des Autopiloten liefen wirklich — nicht gestellt.
    arten = [e["kind"] for e in buch.events(kennung)]
    for pflicht in ("phase_started", "phase_finished"):
        require(pflicht in arten, f"{pflicht} fehlt im Autopilot-Buch: {arten}")
    require(lead.calls >= 1, "der Technical Lead wurde nie gefragt")


def t_no_state_in_this_flow_was_set_by_the_test():
    """Die Gegenprobe zur Gegenprobe.

    Ohne Treiber bleibt derselbe Auftrag stehen, wo er angelegt wurde. Damit
    ist belegt, dass die Zustandsfolge oben vom Treiber kommt und nicht aus
    der Anordnung.
    """
    orch, _router, buch, _bauer, _lead, _werk = _mit_treiber()
    orch.driver_factory = None
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    require_equal(buch.milestone(ende.development_ref).state, A.PLANNING,
                  "ohne Treiber bewegte sich der Auftrag trotzdem")


def t_two_ticks_never_start_two_drivers():
    """Uebernahme und Neustart erzeugen keine doppelte Ausfuehrung.

    Drei Sperren, und die dritte ist die Datei-Sperre des Autopiloten: sie
    haelt auch gegen das Bedienskript und gegen einen zweiten Core.
    """
    orch, _router, buch, bauer, _lead, _werk = _mit_treiber()
    _task, run = V1A._create(orch, objective=ZIEL)
    V1A._run(orch.tick())
    V1A._run(orch.tick())                      # -> parkt, Treiber startet

    async def zweimal():
        a = await orch._start_driver(orch.ledger.get_run(run.run_id).development_ref)
        b = await orch._start_driver(orch.ledger.get_run(run.run_id).development_ref)
        for aufgabe in list(orch._drivers.values()):
            if not aufgabe.done():
                await aufgabe
        return a, b

    erst, zweit = V1A._run(zweimal())
    require(not zweit, "ein zweiter Treiber wurde fuer denselben Auftrag gestartet")
    require_equal(len(orch._drivers), 1, "zwei Treiber im Verzeichnis")


def t_a_foreign_lock_holder_keeps_the_core_out():
    """Faehrt das Bedienskript, faehrt der Core nicht."""
    orch, _router, buch, _bauer, _lead, _werk = _mit_treiber()
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    for aufgabe in list(orch._drivers.values()):
        if not aufgabe.done():
            V1A._run(aufgabe)
    orch._drivers.clear()

    fremd = D._open_lock()                      # jemand anderes faehrt
    try:
        gestartet = V1A._run(orch._start_driver(ende.development_ref))
        require(not gestartet, "der Core startete trotz fremder Sperre")
    finally:
        fremd.close()


def t_a_cancelled_order_takes_its_driver_with_it():
    """Ein abgebrochener Lauf laesst keinen Treiber zurueck — und keine Sperre."""
    orch, _router, buch, _bauer, _lead, _werk = _mit_treiber()
    _task, run = V1A._create(orch, objective=ZIEL)
    _bis_zum_parken(orch, run.run_id)
    require(V1A._run(orch.cancel(run.run_id)), "der Abbruch scheiterte")
    require_equal(orch._drivers, {}, "ein Treiber blieb zurueck")
    # Und die Sperre ist frei: der naechste Auftrag kaeme durch.
    frei = D._open_lock()
    frei.close()


def t_a_worker_that_never_appears_does_not_wait_forever():
    """Faehrt gar niemand, endet der Auftrag — nicht das Warten.

    Der Treiber selbst haengt nicht; er ist durch `MAX_ROUNDS` begrenzt. Diese
    Grenze gilt dem anderen Fall: es erscheint keiner.
    """
    orch, _router, buch, _bauer, _lead, _werk = _mit_treiber()
    orch.driver_factory = None                  # niemand faehrt
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    require_equal(ende.state, S.WAITING_CAPABILITY)

    # Der Auftrag ist aelter als die Wartegrenze — ohne die Uhr zu stellen:
    # das Buch traegt `created_at`, und das laesst sich lesen wie schreiben.
    alt = time.time() - DEV.MAX_WAIT_SECONDS - 60
    buch._db.execute("UPDATE milestones SET created_at=? WHERE milestone_id=?",
                     (alt, ende.development_ref))
    V1A._run(orch.tick())
    danach = orch.ledger.get_run(run.run_id)
    require(danach.state in S.TERMINAL_STATES,
            f"der Lauf wartet weiter: {danach.state}")
    require_equal(danach.failure_category, "timeout")


def t_the_whole_chain_ends_in_the_resumed_user_order():
    """**Der ganze Weg, in einem Stueck.**

    Luecke -> Auftrag -> der vorhandene Treiber faehrt -> Bereitstellung im
    Testsystem -> derselbe Nutzerauftrag laeuft weiter und ruft die neue
    Faehigkeit.
    """
    orch, router, buch, bauer, _lead, _werk = _mit_treiber()
    task, run = V1A._create(orch, objective=ZIEL)
    V1A._run(_drehen(orch, run.run_id))

    kennung = orch.ledger.get_run(run.run_id).development_ref
    require_equal(buch.milestone(kennung).state, A.READY, "der Bau kam nicht durch")
    require(bauer.calls >= 1, "der Builder wurde nie gerufen")

    # Gebaut ist nicht bereitgestellt: der Lauf steht an der Owner-Grenze.
    require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER,
                  "der Lauf lief ohne Bereitstellung weiter")

    router.bereitgestellt = True                 # Bereitstellung im Testsystem
    require(V1A._run(orch.resume(run.run_id)))
    for _ in range(6):
        V1A._run(orch.tick())
        if orch.ledger.get_run(run.run_id).state in S.TERMINAL_STATES:
            break
    require_equal([c["name"] for c in router.calls], [FEHLT, FEHLT],
                  "der urspruengliche Auftrag wurde nicht fortgesetzt")
    require_equal(orch.ledger.get_run(run.run_id).task_id, task.task_id,
                  "es ist nicht mehr derselbe Auftrag")


def t_a_later_gap_finds_the_earlier_development():
    """Technische Historie wird im TECHNISCHEN Buch wiedergefunden.

    Kein neuer Speicher, keine Gedaechtnisautoritaet, keine Vermischung mit
    persoenlichen Erinnerungen: das Autopilot-Buch traegt Vertrag, Zustand und
    Commit ohnehin — gesucht wird dort.
    """
    orch, _router, buch, _b, _l, _w = _mit_treiber()
    _task, erster = V1A._create(orch, objective=ZIEL)
    V1A._run(_drehen(orch, erster.run_id))
    erste_kennung = orch.ledger.get_run(erster.run_id).development_ref
    require_equal(buch.milestone(erste_kennung).state, A.READY)

    # Ein ZWEITER Lauf an derselben Luecke — eigener Auftrag, aber er kennt
    # den ersten.
    _task2, zweiter = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, zweiter.run_id)
    require(ende.development_ref and ende.development_ref != erste_kennung,
            "der zweite Lauf bekam denselben Auftrag")
    vertrag = buch.milestone(ende.development_ref).contract_json
    require("Fruehere Entwicklungen" in vertrag,
            "der neue Auftrag kennt die frueheren nicht")
    require(erste_kennung in vertrag,
            f"der frueher Auftrag fehlt: {vertrag[-300:]}")
    require("READY" in vertrag, "der Ausgang der frueheren Entwicklung fehlt")


def t_technical_history_never_reaches_personal_memory():
    """Die Trennung, an der Quelle geprueft.

    Die Naht liest aus dem Gedaechtnis und schreibt nie hinein; die Historie
    kommt aus dem Autopilot-Buch. Ein Bauprotokoll hat in den persoenlichen
    Erinnerungen nichts zu suchen.
    """
    import ast
    quelle = open(os.path.join(os.path.dirname(DEV.__file__),
                               "development.py"), encoding="utf-8").read()
    baum = ast.parse(quelle)
    gerufen = {n.func.attr for n in ast.walk(baum)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    require("milestones" in gerufen, "die Historie kommt nicht aus dem Autopilot-Buch")
    require("search" in gerufen, "der Gedaechtnis-Leseweg fehlt")
    for verboten in ("remember", "add_record", "upsert", "write"):
        require(verboten not in gerufen, f"die Naht ruft {verboten}")


# =====================================================================
# Der TATSAECHLICHE Aufbauweg des Cores
#
# Der Anschlussfehler in e3d2011: der Orchestrator erwartete eine
# Treiberfabrik, die Abnahme setzte sie am Objekt — und
# `realtime/core_server.py` uebergab gar keine. Der automatische Start gab es
# damit ausschliesslich in der Testanordnung.
#
# Diese Sektion setzt `driver_factory` NICHT nachtraeglich. Sie baut die
# Fabrik ueber `DEV.build_driver_factory` — dieselbe Funktion, die der Core
# ruft — und uebergibt sie dem Orchestrator im Konstruktor, so wie er sie
# bekommt. Gestellt sind nur die beiden aeusseren Abhaengigkeiten.
# =====================================================================

from solvio.agent_runtime.workspace import WorkspaceManager     # noqa: E402


def _testprojekt() -> str:
    """Ein eigenes kleines Projekt — keine Produktionsdaten.

    `WorkspaceManager` klont nur aus seiner Allowlist; `SOLVIO_AGENT_REPOS`
    ist ihr Schalter. Damit klont die Abnahme ausschliesslich dieses Projekt,
    nie den produktiven Baum.
    """
    # AUFGELOEST: die Allowlist des `WorkspaceManager` vergleicht `realpath`,
    # und unter macOS ist `/var/...` ein Symlink auf `/private/var/...`. Der
    # nicht aufgeloeste Pfad faellt mit `repository_not_allowed` durch — die
    # Schranke hat recht, nur meine Uebergabe war falsch.
    projekt = os.path.realpath(APD._arbeitsbereich(gate=APD.GATE_ROT))
    os.environ["SOLVIO_AGENT_REPOS"] = projekt
    return projekt


def _wie_der_core(*, projekt):
    """Der Aufbau, Zeile fuer Zeile wie in `core_server.py`."""
    buch = _buch()
    bauer = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    fabrik = DEV.build_driver_factory(
        ledger=buch, workspaces=WorkspaceManager(allowed=(projekt,)),
        repo=projekt, python=sys.executable,
        adapters={"synthetic": bauer}, lead=_Lead())
    router = Router()
    orch = V1A.O.Orchestrator(
        ledger=V1A._ledger(),
        planner=V1A.Planner([[V1A._cap(FEHLT, titel="Zahnarzt")]]),
        router=router,
        gap_resolver=GapResolver(CapabilityInventory(router)),
        development=buch, driver_factory=fabrik)
    return orch, router, buch, bauer


def t_the_core_build_path_starts_the_developer_by_itself():
    """**Der verlangte Nachweis.**

    Nutzerauftrag -> Luecke -> automatischer Treiberstart -> Builder-Aufruf.
    Ohne nachtraeglich gesetzte Fabrik, ueber `build_driver_factory` und den
    Konstruktor des Orchestrators.
    """
    projekt = _testprojekt()
    orch, _router, buch, bauer = _wie_der_core(projekt=projekt)
    require(orch.driver_factory is not None,
            "der Aufbau lieferte keine Treiberfabrik")

    task, run = V1A._create(orch, objective=ZIEL)
    V1A._run(_drehen(orch, run.run_id))

    kennung = orch.ledger.get_run(run.run_id).development_ref
    require(kennung, "es entstand kein Entwicklungsauftrag")
    require(bauer.calls >= 1,
            "der Builder wurde nie gerufen — der Treiber startete nicht")
    require_equal(buch.milestone(kennung).state, A.READY,
                  f"der Auftrag kam nicht durch: {buch.milestone(kennung).state}")
    require(task.task_id in buch.milestone(kennung).contract_json,
            "die Zuordnung zum Nutzerauftrag fehlt")


def t_the_factory_clones_only_the_test_project():
    """Der Arbeitsbereich ist isoliert und stammt nicht aus der Produktion."""
    projekt = _testprojekt()
    orch, _router, _buch, _bauer = _wie_der_core(projekt=projekt)
    _task, run = V1A._create(orch, objective=ZIEL)
    V1A._run(_drehen(orch, run.run_id))
    kennung = orch.ledger.get_run(run.run_id).development_ref

    from solvio.agent_runtime import workspace as W
    klon = os.path.join(W.workspace_root(kennung), "repo")
    require(os.path.isdir(klon), f"kein isolierter Arbeitsbereich: {klon}")
    require(os.path.realpath(klon).startswith(os.path.realpath(_SANDBOX)),
            f"der Arbeitsbereich liegt ausserhalb des Sandkastens: {klon}")
    # Und er hat kein Fernziel — ein `push` von innen ginge nirgendwohin.
    import subprocess
    fern = subprocess.run(["git", "remote"], cwd=klon, capture_output=True,
                          text=True).stdout.strip()
    require_equal(fern, "", f"der Klon behielt ein Fernziel: {fern}")


def t_the_core_really_passes_a_driver_factory():
    """**Die Zusicherung, die den Fehler von e3d2011 gefangen haette.**

    Geprueft wird der Aufbau im Core an der Quelle: uebergibt er
    `driver_factory`, und kommt sie aus `build_driver_factory`? Eine Naht, die
    nur im Test existiert, ist keine.
    """
    import ast
    pfad = os.path.join(os.path.dirname(os.path.dirname(DEV.__file__)),
                        "realtime", "core_server.py")
    quelle = open(pfad, encoding="utf-8").read()
    require("build_driver_factory" in quelle,
            "der Core baut die Treiberfabrik nicht")

    baum = ast.parse(quelle)
    treffer = [n for n in ast.walk(baum)
               if isinstance(n, ast.Call)
               and getattr(n.func, "id", "") == "Orchestrator"]
    require(treffer, "im Core wird kein Orchestrator gebaut")
    for aufruf in treffer:
        namen = {k.arg for k in aufruf.keywords}
        for pflicht in ("driver_factory", "development", "gap_resolver"):
            require(pflicht in namen,
                    f"der Core uebergibt {pflicht} nicht: {sorted(namen)}")


def t_the_factory_defaults_to_the_real_builder_and_lead():
    """In der Produktion ist nichts gestellt.

    `adapters=None` und `lead=None` sind die Vorgabe — die Abnahme darf sie
    setzen, der Core tut es nicht.
    """
    import ast
    quelle = open(os.path.join(os.path.dirname(DEV.__file__),
                               "development.py"), encoding="utf-8").read()
    baum = ast.parse(quelle)
    fn = next(n for n in ast.walk(baum)
              if isinstance(n, ast.FunctionDef) and n.name == "build_driver_factory")
    vorgaben = {a.arg: v for a, v in
                zip(fn.args.kwonlyargs, fn.args.kw_defaults)}
    for name in ("adapters", "lead"):
        wert = vorgaben.get(name)
        require(isinstance(wert, ast.Constant) and wert.value is None,
                f"{name} hat keine Vorgabe None")

    # Geprueft wird der AUFRUF, nicht seine Umgebung. Ein Textfenster ab dem
    # ersten Vorkommen von `build_driver_factory` traf den Docstring darueber
    # — und liess eine Mutation durch, die den Builder im Core gestellt haette.
    pfad = os.path.join(os.path.dirname(os.path.dirname(DEV.__file__)),
                        "realtime", "core_server.py")
    kern = ast.parse(open(pfad, encoding="utf-8").read())
    aufrufe = [n for n in ast.walk(kern)
               if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") == "build_driver_factory"]
    require(aufrufe, "der Core ruft build_driver_factory nicht")
    for aufruf in aufrufe:
        namen = {k.arg for k in aufruf.keywords}
        for verboten in ("adapters", "lead"):
            require(verboten not in namen,
                    f"der Core stellt {verboten} — dann waere er nicht mehr echt")


# =====================================================================
# R — Ein ersetzter Versuch und ein belegter Effekt
#
# Zwei Blocker, beide an einem echten Lauf gemessen:
#
# 1. `_do_verify` zaehlt jeden Schritt, der nicht `succeeded` oder `skipped`
#    ist, als „ohne Ergebnis". Der erste Versuch von `note_write` scheiterte an
#    der fehlenden Faehigkeit; nach der Bereitstellung gelang GENAU DERSELBE
#    Schritt. Der Auftrag war getan — und der Lauf scheiterte trotzdem.
# 2. Danach kam `no_supported_fulfilment_contract`: fuer einen Handlungsauftrag
#    gab es keinen Erfuellungsvertrag. `open_external_action` liess ihn
#    ausserdem grundsaetzlich scheitern, auch wenn die Handlung nachgemessen
#    war.
#
# Beide Reparaturen sind eng: die Historie bleibt stehen, und die Beweislast
# fuer eine Handlung liegt beim CORE.
# =====================================================================

import hashlib                                                   # noqa: E402
from solvio.agent_runtime import completion as CO                # noqa: E402
from solvio.agent_runtime import requirements as RQ              # noqa: E402


def _lauf_mit_schritten(schritte):
    """Ein Lauf, dessen Schrittzeilen gestellt sind — fuer `_do_verify`."""
    orch = V1A._orch(ledger=V1A._ledger())
    task = orch.ledger.create_task(objective=ZIEL, scope=S.SCOPE_RESEARCH,
                                   created_origin="local_owner",
                                   created_principal="owner")
    run = orch.ledger.create_run(task_id=task.task_id)
    for seq, attempt, zustand, felder in schritte:
        st = orch.ledger.create_step(run_id=run.run_id, seq=seq, kind="capability",
                                     attempt=attempt, capability=FEHLT)
        orch.ledger.update_step(st.step_id, state=zustand, finished=True, **felder)
    return orch, orch.ledger.steps_for_run(run.run_id)


def t_r_a_superseded_attempt_no_longer_blocks_the_run():
    """**Der gemessene Blocker.** Gescheitert, dann derselbe Schritt gelungen."""
    orch, schritte = _lauf_mit_schritten([
        (1, 1, "failed", {"outcome_reason": "unknown_capability"}),
        (1, 2, "succeeded", {})])
    ersetzt = orch._superseded(schritte)
    require_equal(len(ersetzt), 1, f"der ersetzte Versuch wurde nicht erkannt")
    blockierend = [s for s in schritte
                   if s.state not in orch.SETTLED_OK and s.step_id not in ersetzt]
    require_equal(blockierend, [], "der Lauf wird weiterhin blockiert")
    # Und die Historie steht noch da.
    require_equal(sorted((s.seq, s.attempt, s.state) for s in schritte),
                  [(1, 1, "failed"), (1, 2, "succeeded")],
                  "die Historie wurde veraendert")


def t_r_only_a_harmless_failed_attempt_is_ever_superseded():
    """**Die drei Bedingungen, einzeln gegengeprueft.**

    Faellt eine weg, bleibt der Schritt blockierend. Im Zweifel gilt ein Lauf
    als nicht erfuellt.
    """
    ABBRUCH = {"outcome_reason": "unknown_capability"}
    faelle = [
        ("kein spaeterer Erfolg",
         [(1, 1, "failed", ABBRUCH), (1, 2, "failed", ABBRUCH)]),
        ("ungewisser Ausgang",
         [(1, 1, "unknown", ABBRUCH), (1, 2, "succeeded", {})]),
        ("abgelehnt", [(1, 1, "denied", ABBRUCH), (1, 2, "succeeded", {})]),
        ("Freigabe beansprucht",
         [(1, 1, "failed", {**ABBRUCH, "approval_id": "ap-1"}),
          (1, 2, "succeeded", {})]),
        ("Ausfuehrung beansprucht",
         [(1, 1, "failed", {**ABBRUCH, "execution_id": "ex-1"}),
          (1, 2, "succeeded", {})]),
        ("anderer Schritt gelang",
         [(1, 1, "failed", ABBRUCH), (2, 1, "succeeded", {})]),
        # **Die neue Bedingung.** Ohne bewiesenen Abbruch VOR der Ausfuehrung
        # bleibt der Versuch blockierend — auch ohne jede Kennung. Genau diese
        # Faelle galten vorher als harmlos, obwohl sie draussen gewirkt haben
        # koennen.
        ("Zeitablauf", [(1, 1, "failed", {"outcome_reason": "timeout"}),
                        (1, 2, "succeeded", {})]),
        ("abgebrochen", [(1, 1, "failed", {"outcome_reason": "cancelled"}),
                         (1, 2, "succeeded", {})]),
        ("Grund unbekannt", [(1, 1, "failed", {}), (1, 2, "succeeded", {})]),
    ]
    for name, schritte in faelle:
        orch, zeilen = _lauf_mit_schritten(schritte)
        ersetzt = orch._superseded(zeilen)
        blockierend = [s for s in zeilen
                       if s.state not in orch.SETTLED_OK and s.step_id not in ersetzt]
        require(blockierend, f"{name}: der Schritt blockiert nicht mehr")


def t_r_an_uncertain_step_blocks_even_when_superseded():
    """**Die Zusage, die nicht fallen darf.**

    „Moeglicherweise ausgefuehrt" bleibt moeglicherweise ausgefuehrt — auch
    wenn ein spaeterer Versuch gelang. `_do_verify` liest die ungewissen
    Schritte deshalb aus ALLEN Zeilen, nicht aus den blockierenden.
    """
    import ast, inspect
    quelle = inspect.getsource(V1A.O.Orchestrator._do_verify)
    baum = ast.parse(quelle.lstrip())
    zuweisungen = {}
    for n in ast.walk(baum):
        if not isinstance(n, ast.Assign):
            continue
        name = getattr(n.targets[0], "id", "")
        if name in ("uncertain", "unsettled"):
            zuweisungen[name] = ast.dump(n.value)
    require("ersetzt" not in zuweisungen.get("uncertain", ""),
            "die ungewissen Schritte werden gefiltert — das darf nie sein")
    require("ersetzt" in zuweisungen.get("unsettled", ""),
            "die blockierenden Schritte beruecksichtigen die Ersetzung nicht")


# ---------------------------------------------------------------------
# Der Erfuellungsnachweis fuer Handlungen
# ---------------------------------------------------------------------

def _handlungslage(*, belege, verified):
    """Eine Vertragslage mit EINER Handlungsforderung."""
    roh = {"auskunft": [], "handlungen": [{"id": "h1", "text": "Notiz anlegen"}],
           "unklar": [], "belege": {"mindestens": 0}}
    bound = RQ.validate(roh, objective=ZIEL)
    koerper = RQ.snapshot_body(list({*belege, *verified}), [])
    return {"bound": bound, "snapshot": json.loads(koerper),
            "snapshot_digest": RQ.snapshot_digest(koerper),
            "requirements_digest": RQ.digest_of(bound),
            "task_id": "at-h", "run_id": "ar-h",
            # Beleg → die Anforderung, fuer die der Core ihn gemessen hat.
            "verified_effects": {b: "h1" for b in verified},
            "judgement": {"v": RQ.VERSION, "task_id": "at-h", "run_id": "ar-h",
                          "anforderungen_digest": RQ.digest_of(bound),
                          "snapshot": RQ.snapshot_digest(koerper),
                          "beantwortet": [{"id": "h1", "belege": list(belege)}],
                          "offen": [], "fehlend": [], "unsicher": [],
                          "weiterarbeit_noetig": False}}


BELEG = "note_write → /tmp/n.txt: Zahnarzt Dienstag 9 Uhr"


def t_r_an_action_needs_an_effect_the_core_verified_itself():
    """Eine Handlung gilt nur als erfuellt, wenn der CORE nachgelesen hat."""
    gut = CO.information(**_handlungslage(belege=[BELEG], verified=[BELEG]))
    require(gut.satisfied, f"ein verifizierter Effekt traegt nicht: {gut.reason}")
    require_equal(gut.contract, CO.INFORMATION)


def t_r_a_model_cannot_talk_an_action_into_being_done():
    """**Die eigentliche Zusage.** Eine Rechercheszeile deckt keine Handlung."""
    erzaehlt = "Laut Recherche wurde die Notiz angelegt."
    urteil = CO.information(**_handlungslage(belege=[erzaehlt], verified=[]))
    require(not urteil.satisfied, "ein erzaehlter Beleg hat eine Handlung gedeckt")
    require_equal(urteil.reason, "action_not_verified",
                  f"falscher Grund: {urteil.reason}")


def t_r_an_unmentioned_action_still_blocks_as_before():
    """Wird die Handlung gar nicht zugeordnet, bleibt es bei der alten Schranke."""
    lage = _handlungslage(belege=[BELEG], verified=[BELEG])
    lage["judgement"]["beantwortet"] = []
    urteil = CO.information(**lage)
    require(not urteil.satisfied, "eine unerwaehnte Handlung hat getragen")
    require_equal(urteil.reason, "open_external_action")


def t_r_a_mixed_order_needs_every_action_verified():
    """Gemischte Auftraege gelingen erst bei VOLLSTAENDIGER Erfuellung."""
    roh = {"auskunft": [{"id": "a1", "text": "Wann ist der Termin?"}],
           "handlungen": [{"id": "h1", "text": "Notiz anlegen"},
                          {"id": "h2", "text": "Termin absagen"}],
           "unklar": [], "belege": {"mindestens": 0}}
    bound = RQ.validate(roh, objective=ZIEL)
    antwort = "Der Termin ist am Dienstag."
    koerper = RQ.snapshot_body([antwort, BELEG], [])
    lage = {"bound": bound, "snapshot": json.loads(koerper),
            "snapshot_digest": RQ.snapshot_digest(koerper),
            "requirements_digest": RQ.digest_of(bound),
            "task_id": "at-m", "run_id": "ar-m",
            "verified_effects": {BELEG: "h1"},
            "judgement": {"v": RQ.VERSION, "task_id": "at-m", "run_id": "ar-m",
                          "anforderungen_digest": RQ.digest_of(bound),
                          "snapshot": RQ.snapshot_digest(koerper),
                          "beantwortet": [{"id": "a1", "belege": [antwort]},
                                          {"id": "h1", "belege": [BELEG]}],
                          "offen": [], "fehlend": [], "unsicher": [],
                          "weiterarbeit_noetig": False}}
    urteil = CO.information(**lage)
    require(not urteil.satisfied, "eine offene zweite Handlung hat getragen")
    require_equal(urteil.reason, "open_external_action")
    require("absagen" in " ".join(urteil.open_points), urteil.open_points)


def t_r_a_pure_action_order_can_be_bound_at_all():
    """Ein reiner Handlungsauftrag ist ein Auftrag — ein leerer Satz nicht."""
    RQ.validate({"auskunft": [], "handlungen": [{"id": "h1", "text": "x"}],
                 "unklar": [], "belege": {"mindestens": 0}}, objective=ZIEL)
    geworfen = ""
    try:
        RQ.validate({"auskunft": [], "handlungen": [], "unklar": [],
                     "belege": {"mindestens": 0}}, objective=ZIEL)
    except RQ.RequirementsInvalid as exc:
        geworfen = exc.reason
    require_equal(geworfen, "no_requirement", "ein leerer Satz galt als Vertrag")


def t_r_too_many_build_rounds_stop_the_order():
    """Ein kleiner Auftrag loest keine beliebig vielen Entwicklungsrunden aus.

    `Driver.run` kehrt nach `MAX_ROUNDS` zurueck, auch unfertig — und der Takt
    wuerde es sonst endlos neu starten. Gemessen am echten Notizlauf: ZWEI
    Starts, 16 Bauphasen, 17 Modellurteile in 23 Minuten. Nur die
    Sechs-Stunden-Uhr haette gestoppt.
    """
    orch, _router, buch = _aufbau()
    orch.driver_factory = lambda mid="": None      # es faehrt niemand weiter
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    require_equal(ende.state, S.WAITING_CAPABILITY)

    # So viele Bauphasen, wie die Grenze erlaubt — direkt ins Buch, weil es
    # hier um die GRENZE geht und nicht um den Bau.
    for _ in range(DEV.MAX_BUILD_PHASES):
        buch.start_phase(ende.development_ref, kind="build", builder="synthetic")
    require_equal(DEV.build_phases(buch, ende.development_ref),
                  DEV.MAX_BUILD_PHASES, "die Zaehlung stimmt nicht")

    V1A._run(orch.tick())
    danach = orch.ledger.get_run(run.run_id)
    require(danach.state in S.TERMINAL_STATES,
            f"der Lauf laeuft weiter: {danach.state}")
    require_equal(danach.failure_category, "budget_exhausted")


def t_r_the_round_limit_counts_from_the_book_not_from_memory():
    """Die Grenze ueberlebt den Neustart — sie zaehlt im Buch.

    Ein Zaehler im Arbeitsspeicher waere nach einem Prozessverlust wieder
    null, und genau ueber Neustarts hinweg soll sie halten.
    """
    orch, _router, buch = _aufbau()
    _task, run = V1A._create(orch, objective=ZIEL)
    ende = _bis_zum_parken(orch, run.run_id)
    for _ in range(3):
        buch.start_phase(ende.development_ref, kind="build", builder="synthetic")
    require_equal(DEV.build_phases(buch, ende.development_ref), 3)
    # Ein frischer Orchestrator sieht dieselbe Zahl.
    frisch = V1A._orch(ledger=orch.ledger)
    frisch.development = buch
    require_equal(DEV.build_phases(frisch.development, ende.development_ref), 3,
                  "ein Neustart setzt die Zaehlung zurueck")
    # Ein unlesbares Buch erlaubt nichts.
    require_equal(DEV.build_phases(object(), ende.development_ref),
                  DEV.MAX_BUILD_PHASES,
                  "ein unlesbares Buch gilt als Erlaubnis")


def t_r_the_closing_message_fits_the_contract_not_the_scope():
    """Ein Handlungsauftrag meldet, was erledigt ist — kein Bauergebnis.

    Gemessen am echten Lauf: dort stand „Das Ergebnis liegt als  bereit —
    uebernehmen ist deine Entscheidung." Ein leerer Platzhalter und die Bitte
    um eine Entscheidung, die es nicht gibt.
    """
    from solvio.agent_runtime.orchestrator import _erledigt_satz
    require_equal(_erledigt_satz([]), "Ich habe alles erledigt, was du wolltest.")
    einer = _erledigt_satz(["Die Notiz anlegen"])
    require_equal(einer, "Erledigt: Die Notiz anlegen.")
    require("bereit" not in einer and "Entscheidung" not in einer,
            f"die Bau-Meldung steckt noch drin: {einer}")
    mehrere = _erledigt_satz(["A", "B"])
    require("A" in mehrere and "B" in mehrere, mehrere)




# =====================================================================
# S — Der Ausfuehrungsbeleg selbst
#
# Abschnitt R hat gezeigt, dass eine Handlung einen vom Core gemessenen Beleg
# braucht. Er hat NICHT gezeigt, wie dieser Beleg entsteht — und die erste
# Fassung entstand zu leicht: `_record_effect` oeffnete `outcome.data["pfad"]`
# ungeprueft und schrieb, was drinstand, als Beleg fort.
#
# Zwei Loecher, beide ohne Modellboshaftigkeit erreichbar:
#
# * **Jede Datei wurde zum Bewertungskontext.** Eine Faehigkeit, die
#   `pfad=/etc/hosts` meldet, haette deren Inhalt in `findings` und `sources`
#   gebracht — in denselben Snapshot, aus dem das Modell zitiert.
# * **Vorhandener Inhalt galt als Nachweis.** Wer eine Zeile findet, die schon
#   gestern dastand, hat nichts angehaengt. Der Beleg sah trotzdem gleich aus.
#
# Diese Sektion prueft die fuenf Bedingungen einzeln, jede mit einer eigenen
# Gegenprobe, und zuletzt, dass der richtig ausgefuehrte Notizauftrag weiter
# gelingt. Alles deterministisch: kein Builder, kein Modell, kein Netz.
# =====================================================================

class _Ergebnis:
    """Was eine Faehigkeit zurueckmeldet — hier gestellt, sonst vom Router."""

    def __init__(self, pfad, *, state="succeeded"):
        self.state = state
        self.data = {"pfad": pfad}
        self.human_message = "erledigt"
        self.call_id = "c-eff"
        self.approval_id = ""
        self.execution_id = ""
        self.outcome_reason = ""
        self.failure_category = ""


class _Schritt:
    """Ein geplanter Schritt mit gebundenen Argumenten und Anforderung."""

    def __init__(self, *, pfad, text, requirement="h1"):
        self.kind = "capability"
        self.capability = "note_write"
        self.arguments = {"pfad": pfad, "text": text}
        self.requirement = requirement
        self.optional = False
        self.profile = ""
        self.instruction = ""


def _effektlage(*, requirement="h1"):
    """Ein Lauf mit einem laufenden Schritt — bereit fuer `_record_effect`."""
    orch = V1A._orch(ledger=V1A._ledger())
    task = orch.ledger.create_task(objective=ZIEL, scope=S.SCOPE_RESEARCH,
                                   created_origin="local_owner",
                                   created_principal="owner")
    run = orch.ledger.create_run(task_id=task.task_id)
    step = orch.ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                   attempt=1, capability="note_write")
    kontext = V1A.O.RunContext(run_id=run.run_id, task_id=task.task_id,
                               scope=S.SCOPE_RESEARCH,
                               ledger=V1A.O.BU.BudgetLedger(
                                   budget=V1A.O.BU.DEFAULTS["research"]))
    return orch, run, kontext, step


def _im_zustand(name: str) -> str:
    """Ein Pfad IM Zustandsordner des Cores — dem einzigen erlaubten Ort."""
    wurzel = os.path.join(os.path.realpath(os.environ["SOLVIO_STATE_DIR"]),
                          V1A.O.Orchestrator.EFFECT_DIR)
    os.makedirs(wurzel, exist_ok=True)
    return os.path.join(wurzel, name)


def _belege(orch, run, kontext, step, planned, pfad):
    """Einen Effekt buchen und zurueckgeben, was der Core daraus machte."""
    V1A._run(orch._record_effect(run, kontext, step, planned, _Ergebnis(pfad)))
    return orch._verified_effects(run.run_id)


NOTIZ = "Zahnarzt Dienstag 9 Uhr"


def t_s_the_correctly_executed_note_order_still_succeeds():
    """**Der Fall, der weiter gelingen MUSS.**

    Angehaengt an eine vorhandene Datei, im Zustandsordner, mit dem
    freigegebenen Text. Das ist der Lauf, den der echte Builder erzeugt hat —
    er darf durch keine der neuen Schranken fallen.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("notizen.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(len(belege), 1, f"kein Beleg entstanden: {belege}")
    beleg = next(iter(belege))
    require_equal(belege[beleg], "h1", "der Beleg ist keiner Handlung zugeordnet")
    require("append" in beleg, f"die Operation fehlt im Beleg: {beleg}")
    require(NOTIZ in beleg, f"der Inhalt fehlt im Beleg: {beleg}")
    require(beleg in kontext.findings, "der Beleg fehlt im Bewertungskontext")

    # Und er traegt den ganzen Weg bis zum Vertrag.
    lage = _handlungslage(belege=[beleg], verified=[])
    lage["verified_effects"] = belege
    koerper = RQ.snapshot_body([beleg], [])
    lage["snapshot"] = json.loads(koerper)
    lage["snapshot_digest"] = RQ.snapshot_digest(koerper)
    lage["judgement"]["snapshot"] = RQ.snapshot_digest(koerper)
    urteil = CO.information(**lage)
    require(urteil.satisfied, f"der belegte Notizauftrag scheitert: {urteil.reason}")


def t_s_a_target_outside_the_state_folder_is_never_even_read():
    """**Gegenprobe 1: falscher Zielpfad.**

    Der Ausfuehrer nennt eine Datei ausserhalb des Zustandsordners. Sie darf
    weder einen Beleg erzeugen NOCH in den Bewertungskontext geraten — sonst
    machte `pfad` beliebige Dateien zum Modellkontext.

    **Der Fall ist absichtlich der schwerste:** ausserhalb der Wurzel findet
    ein ECHTES, sauberes Anhaengen statt. Jede andere Schranke — autorisiertes
    Ziel, Operation, Nutzlast — waere hier zufrieden. Nur die Wurzel kann das
    ablehnen. Eine fruehere Fassung liess kein Anhaengen stattfinden und blieb
    deshalb auch dann gruen, wenn man die Wurzelschranke entfernte; gemessen
    mit genau dieser Mutation.
    """
    orch, run, kontext, step = _effektlage()
    fremd = os.path.join(_SANDBOX, "fremd.txt")
    with open(fremd, "w", encoding="utf-8") as fh:
        fh.write("GEHEIM: was hier steht, geht den Bewerter nichts an\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=fremd, text=NOTIZ))
    with open(fremd, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")          # ein tadelloses Anhaengen — am falschen Ort

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=fremd, text=NOTIZ), fremd)
    require_equal(belege, {}, "eine fremde Datei hat einen Beleg erzeugt")
    require_equal(kontext.findings, [], "eine fremde Datei kam in den Kontext")
    require_equal(kontext.sources, [], "eine fremde Datei kam in die Quellen")
    require_equal(kontext.effect_before, {},
                  "das Vorher-Bild hat eine Datei ausserhalb der Wurzel gelesen")


def t_s_a_reported_target_may_not_differ_from_the_authorised_one():
    """**Gegenprobe 1b: verschobenes Ziel.**

    Beide Pfade liegen im Zustandsordner, aber der gemeldete ist nicht der
    autorisierte. „Nach einer Freigabe duerfen gebundene Parameter nicht still
    veraendert werden" — auch nicht der Zielort.
    """
    orch, run, kontext, step = _effektlage()
    autorisiert = _im_zustand("erlaubt.md")
    anderes = _im_zustand("woanders.md")
    kontext.effect_before = orch._effect_before(
        _Schritt(pfad=autorisiert, text=NOTIZ))
    with open(anderes, "w", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=autorisiert, text=NOTIZ), anderes)
    require_equal(belege, {}, "ein verschobenes Ziel hat einen Beleg erzeugt")


def t_s_text_that_was_already_there_proves_no_write():
    """**Gegenprobe 2: vorhandener Text ohne Schreibwirkung.**

    Die Zeile steht schon in der Datei, bevor die Faehigkeit laeuft. Danach
    steht sie immer noch da — und genau das ist KEIN Nachweis. Ohne
    Vorher-Bild sahen beide Faelle gleich aus.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("schonda.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n" + NOTIZ + "\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))      # NACH dem Schreiben

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(belege, {}, "vorhandener Text galt als erfolgreiche Handlung")


def t_s_an_overwrite_is_not_an_append():
    """**Gegenprobe 2b: die verlangte Operation.**

    Die Datei wurde ERSETZT, nicht ergaenzt: der alte Inhalt ist fort. Der
    verlangte Text steht drin, die Datei ist gewachsen — und trotzdem ist das
    nicht das, was der Auftrag verlangte.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("ersetzt.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Ein alter Kopf, der bleiben sollte\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "w", encoding="utf-8") as fh:      # ueberschrieben
        fh.write("Voellig neuer Text. " + NOTIZ + "\n")

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(belege, {}, "ein Ueberschreiben galt als Anhaengen")


def t_s_the_wrong_content_proves_nothing():
    """**Gegenprobe 3: falscher Inhalt.**

    Etwas wurde angehaengt — aber nicht das, was der Auftrag verlangte.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("falsch.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write("Irgendetwas anderes\n")

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(belege, {}, "ein fremder Inhalt hat die Handlung belegt")


def t_s_a_tampered_evidence_artifact_stops_counting():
    """**Gegenprobe 4: manipuliertes Belegartefakt.**

    Der Beleg liegt als Datei auf der Platte. Wer sie aendert, aendert den
    Beleg — es sei denn, er wird beim Wiederverwenden nachgerechnet. Genau das
    fehlte: `_verified_effects` las den Satz vorher ungeprueft.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("beleg.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")
    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(len(belege), 1, "der Ausgangsbeleg fehlt")

    artefakte = [a for a in orch.ledger.artifacts_for_run(run.run_id)
                 if a.kind == "action_result"]
    require_equal(len(artefakte), 1, "kein Belegartefakt gebucht")
    satz = json.loads(open(artefakte[0].path, encoding="utf-8").read())
    satz["erwartet"] = "Etwas ganz anderes, das nie geschrieben wurde"
    with open(artefakte[0].path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(satz, ensure_ascii=False, sort_keys=True))

    require_equal(orch._verified_effects(run.run_id), {},
                  "ein manipuliertes Artefakt hat weiter belegt")


def t_s_one_effect_never_covers_a_second_action():
    """**Gegenprobe 5: zwei Handlungen, ein passender Beleg.**

    Der Auftrag verlangt zwei Handlungen; ausgefuehrt und gemessen wurde eine.
    Nennt das Modell denselben Beleg zweimal, darf die zweite Handlung NICHT
    gedeckt sein. Ohne die Bindung an die Anforderungskennung genuegte dem
    alten Mengenvergleich genau dieser Trick.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("zwei.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")
    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ, requirement="h1"), ziel)
    beleg = next(iter(belege))

    roh = {"auskunft": [],
           "handlungen": [{"id": "h1", "text": "Notiz anlegen"},
                          {"id": "h2", "text": "Termin absagen"}],
           "unklar": [], "belege": {"mindestens": 0}}
    bound = RQ.validate(roh, objective=ZIEL)
    koerper = RQ.snapshot_body([beleg], [])
    lage = {"bound": bound, "snapshot": json.loads(koerper),
            "snapshot_digest": RQ.snapshot_digest(koerper),
            "requirements_digest": RQ.digest_of(bound),
            "task_id": "at-2", "run_id": "ar-2", "verified_effects": belege,
            "judgement": {"v": RQ.VERSION, "task_id": "at-2", "run_id": "ar-2",
                          "anforderungen_digest": RQ.digest_of(bound),
                          "snapshot": RQ.snapshot_digest(koerper),
                          # Derselbe Beleg fuer BEIDE Handlungen.
                          "beantwortet": [{"id": "h1", "belege": [beleg]},
                                          {"id": "h2", "belege": [beleg]}],
                          "offen": [], "fehlend": [], "unsicher": [],
                          "weiterarbeit_noetig": False}}
    urteil = CO.information(**lage)
    require(not urteil.satisfied,
            "ein Beleg hat zwei verschiedene Handlungen gedeckt")
    require_equal(urteil.reason, "action_not_verified")
    require("absagen" in " ".join(urteil.open_points), urteil.open_points)


def t_s_an_effect_without_a_requirement_covers_nothing():
    """**Die Bindung ist Pflicht, nicht Zierde.**

    Sagt der Plan nicht, welche Handlung ein Schritt erfuellt, entsteht der
    Beleg trotzdem — er deckt nur nichts. Fail-closed: keine Zuordnung heisst
    offene Handlung, nicht stillschweigende Erfuellung.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("ungebunden.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")
    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ, requirement=""), ziel)
    beleg = next(iter(belege))
    require_equal(belege[beleg], "", "eine Bindung entstand aus dem Nichts")

    lage = _handlungslage(belege=[beleg], verified=[])
    lage["verified_effects"] = belege
    koerper = RQ.snapshot_body([beleg], [])
    lage["snapshot"] = json.loads(koerper)
    lage["snapshot_digest"] = RQ.snapshot_digest(koerper)
    lage["judgement"]["snapshot"] = RQ.snapshot_digest(koerper)
    urteil = CO.information(**lage)
    require(not urteil.satisfied, "ein ungebundener Beleg hat getragen")
    require_equal(urteil.reason, "action_not_verified")


def t_s_the_evidence_is_bound_to_task_step_and_attempt():
    """**Der Beleg ist dauerhaft und gebunden — nicht ein Satz im Kontext.**

    Aufgabe, Lauf, Schritt, Versuch, Faehigkeit, Anforderung, Ziel, Operation
    und geprueftes Merkmal stehen im Artefakt. Ohne sie waere spaeter nicht
    mehr feststellbar, WAS eigentlich nachgewiesen wurde.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("gebunden.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")
    _belege(orch, run, kontext, step, _Schritt(pfad=ziel, text=NOTIZ), ziel)

    artefakt = [a for a in orch.ledger.artifacts_for_run(run.run_id)
                if a.kind == "action_result"][0]
    koerper = open(artefakt.path, encoding="utf-8").read()
    require_equal(hashlib.sha256(koerper.encode("utf-8")).hexdigest(),
                  artefakt.sha256, "der gebuchte Hash passt nicht zum Artefakt")
    satz = json.loads(koerper)
    for feld, erwartet in (("task_id", run.task_id), ("run_id", run.run_id),
                           ("step_id", step.step_id), ("attempt", 1),
                           ("capability", "note_write"), ("requirement", "h1"),
                           ("ziel", ziel), ("operation", "append"),
                           ("erwartet", NOTIZ)):
        require_equal(satz.get(feld), erwartet, f"{feld} fehlt oder ist falsch")
    require_equal(orch.ledger.get_step(step.step_id).artifact_refs,
                  [artefakt.artifact_id], "der Schritt verweist nicht auf den Beleg")


def t_s_an_effect_of_another_run_is_never_borrowed():
    """Ein Beleg gehoert zu SEINEM Lauf. Ein fremder zaehlt nicht mit."""
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("fremdlauf.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")
    _belege(orch, run, kontext, step, _Schritt(pfad=ziel, text=NOTIZ), ziel)
    artefakt = [a for a in orch.ledger.artifacts_for_run(run.run_id)
                if a.kind == "action_result"][0]

    anderer = orch.ledger.create_run(task_id=run.task_id)
    orch.ledger.add_artifact(run_id=anderer.run_id, kind="action_result",
                             path=artefakt.path, sha256=artefakt.sha256,
                             size=artefakt.bytes)
    require_equal(orch._verified_effects(anderer.run_id), {},
                  "der Beleg eines fremden Laufs wurde uebernommen")


def t_s_a_growing_file_does_not_prove_the_line_was_written_now():
    """**Gegenprobe 2c — die Luecke, die eine Mutation offengelegt hat.**

    Der schaerfste Fall, und der einzige, den die anderen nicht abdecken: die
    Datei WAECHST (irgendetwas wurde angehaengt), aber der verlangte Text stand
    schon vorher drin. Wer nur fragt „steht der Text jetzt in der Datei?",
    sagt hier ja — und belegt eine Handlung, die nie stattfand.

    Gemessen: eine Mutation, die `zuwachs` gegen `nachher` tauschte, ueberlebte
    alle uebrigen Gegenproben dieser Sektion. Erst dieser Fall toetet sie.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("gewachsen.md")
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n" + NOTIZ + "\n")     # steht SCHON da
    kontext.effect_before = orch._effect_before(_Schritt(pfad=ziel, text=NOTIZ))
    with open(ziel, "a", encoding="utf-8") as fh:
        fh.write("Eine ganz andere Zeile\n")        # die Datei waechst

    belege = _belege(orch, run, kontext, step,
                     _Schritt(pfad=ziel, text=NOTIZ), ziel)
    require_equal(belege, {},
                  "ein fremder Zuwachs hat eine alte Zeile zum Nachweis gemacht")


def t_s_the_same_wording_for_two_requirements_covers_neither():
    """**Wenn ein Beleg zu zwei Anforderungen passt, deckt er keine.**

    Der Belegtext besteht aus Faehigkeit, Ziel, Operation und Nutzlast. Zwei
    verschiedene Anforderungen koennen darin zusammenfallen. Dann ist nicht
    entscheidbar, welche er deckt — und ein stilles Ueberschreiben der
    Zuordnung haette die eine zufaellig gedeckt und die andere zufaellig offen
    gelassen, je nach Lesereihenfolge im Buch.
    """
    orch, run, kontext, step = _effektlage()
    ziel = _im_zustand("doppelt.md")
    marken = []
    for anforderung in ("h1", "h2"):
        with open(ziel, "w", encoding="utf-8") as fh:
            fh.write("# Notizen\n")
        planned = _Schritt(pfad=ziel, text=NOTIZ, requirement=anforderung)
        kontext.effect_before = orch._effect_before(planned)
        with open(ziel, "a", encoding="utf-8") as fh:
            fh.write(NOTIZ + "\n")
        zweiter = orch.ledger.create_step(
            run_id=run.run_id, seq=len(marken) + 2, kind="capability",
            attempt=1, capability="note_write")
        V1A._run(orch._record_effect(run, kontext, zweiter, planned,
                                     _Ergebnis(ziel)))
        marken.append(anforderung)

    belege = orch._verified_effects(run.run_id)
    require_equal(len(belege), 1, f"die Marken fielen nicht zusammen: {belege}")
    require_equal(next(iter(belege.values())), "",
                  "eine der beiden Anforderungen wurde zufaellig gedeckt")


def t_s_the_state_folder_itself_is_not_a_permitted_target():
    """**Der Zustandsordner ist keine erlaubte Wirkungsflaeche.**

    Gemessen an der produktiven Anlage liegen in `~/.solvio` unter anderem
    `contacts.sqlite3`, `conversations.sqlite3`, `memory`,
    `payment-sandbox.env` und `satellite_auth.json`. Eine erste Fassung liess
    den ganzen Ordner als Zielwurzel zu — ein gemeldetes Ziel haette den Core
    dazu gebracht, daraus zu lesen und einen Auszug in den Bewertungssnapshot
    zu legen, also in den Modellkontext.

    Erlaubt ist nur der Unterordner, den SOLVIO fuer Wirkungen haelt. Auch
    hier findet der schwerste Fall statt: ein tadelloses Anhaengen, nur am
    falschen Ort.
    """
    orch, run, kontext, step = _effektlage()
    daneben = os.path.join(os.path.realpath(os.environ["SOLVIO_STATE_DIR"]),
                           "satellite_auth.json")
    with open(daneben, "w", encoding="utf-8") as fh:
        fh.write('{"token": "GEHEIMNIS"}\n')
    planned = _Schritt(pfad=daneben, text=NOTIZ)
    kontext.effect_before = orch._effect_before(planned)
    with open(daneben, "a", encoding="utf-8") as fh:
        fh.write(NOTIZ + "\n")

    belege = _belege(orch, run, kontext, step, planned, daneben)
    require_equal(belege, {}, "eine Datei neben dem Wirkungsordner hat belegt")
    require_equal(kontext.effect_before, {},
                  "das Vorher-Bild hat im Zustandsordner gelesen")
    require("GEHEIMNIS" not in json.dumps(kontext.findings + kontext.sources),
            "Zustandsinhalt ist in den Bewertungskontext ausgetreten")


def t_r_a_structurally_invalid_step_never_reached_the_router():
    """**Der zweite bewiesene Abbruchgrund — vom ersten Live-Lauf gefunden.**

    Der echte Planer traf im ersten Anlauf die Pflichtangabe `pfad` nicht.
    `_structural_flaw` lehnte den Schritt ab, der Core schrieb ihm den Mangel
    in den Kontext, der zweite Versuch gelang — und der Lauf scheiterte
    trotzdem, weil `planner_invalid_step` nicht in `NO_EFFECT_REASONS` stand.

    Der Abbruch ist hier STAERKER belegt als bei `unknown_capability`: die
    Pruefung steht am Anfang von `_run_capability_step`, vor dem Vorher-Bild
    und vor `execute_capability`. Gemessen am echten Lauf trug der Versuch
    weder `call_id` noch Freigabe- noch Ausfuehrungskennung.
    """
    orch, schritte = _lauf_mit_schritten([
        (1, 1, "failed", {"outcome_reason":
                          "planner_invalid_step:missing_argument:pfad"}),
        (1, 2, "succeeded", {})])
    ersetzt = orch._superseded(schritte)
    blockierend = [s for s in schritte
                   if s.state not in orch.SETTLED_OK and s.step_id not in ersetzt]
    require_equal(blockierend, [],
                  "ein strukturell ungueltiger Versuch blockiert weiter")


def t_r_the_reason_head_is_compared_not_a_prefix():
    """**Der Kopf, nicht der Anfang.** Ein erfundener Grund darf nicht passen.

    `startswith` haette `unknown_capability_but_executed` durchgelassen — einen
    Grund, der ausdruecklich sagt, dass ausgefuehrt wurde. Verglichen wird
    deshalb der Teil VOR dem ersten Doppelpunkt gegen eine geschlossene Menge.
    """
    for grund in ("unknown_capability_but_executed",
                  "planner_invalid_step_after_write",
                  "unknown_capabilityX"):
        orch, schritte = _lauf_mit_schritten([
            (1, 1, "failed", {"outcome_reason": grund}), (1, 2, "succeeded", {})])
        ersetzt = orch._superseded(schritte)
        blockierend = [s for s in schritte
                       if s.state not in orch.SETTLED_OK
                       and s.step_id not in ersetzt]
        require(blockierend, f"{grund!r} galt als bewiesener Abbruch")
    # Und mit Detail hinter dem Doppelpunkt traegt derselbe Grund weiterhin.
    orch, schritte = _lauf_mit_schritten([
        (1, 1, "failed", {"outcome_reason": "unknown_capability:note_write"}),
        (1, 2, "succeeded", {})])
    require_equal(len(orch._superseded(schritte)), 1,
                  "ein Grund mit Detail wurde nicht mehr erkannt")


def t_r_the_closing_message_carries_no_core_notes():
    """**Interne Notizen sind an das MODELL gerichtet, nicht an den Menschen.**

    Gemessen an der ersten Live-Abnahme des Notizauftrags: der Lauf gelang,
    und der Nutzer las

        „[core] Notizdatei: /…/notizen.md [core] note_write fehlte eine
        Pflichtangabe (missing_argument:pfad); so nicht noch einmal
        [core] Ein geplanter Schritt war unvollstaendig Erledigt: …"

    „so nicht noch einmal" ist eine Anweisung an den Planer. Sie im
    Ergebnistext auszuliefern ist keine Offenheit, sondern eine verfehlte
    Adresse.
    """
    orch = V1A._orch(ledger=V1A._ledger())
    kontext = V1A.O.RunContext(
        run_id="ar-m", task_id="at-m", scope=S.SCOPE_RESEARCH,
        ledger=V1A.O.BU.BudgetLedger(budget=V1A.O.BU.DEFAULTS["research"]))
    kontext.context_notes.extend([
        "[core] Notizdatei: /tmp/x/effects/notizen.md",
        "[core] note_write fehlte eine Pflichtangabe (missing_argument:pfad)",
        "Die Recherche ergab: der Termin ist am Dienstag.",
    ])
    satz = orch._summarise(kontext)
    require("[core]" not in satz, f"eine interne Notiz steht in der Meldung: {satz}")
    require("Pflichtangabe" not in satz, f"Planeranweisung ausgeliefert: {satz}")
    require("Dienstag" in satz, f"der echte Befund fehlt: {satz}")

    # Und wenn NUR interne Notizen da sind, bleibt ein ehrlicher Satz stehen —
    # kein leerer String, der wie ein fehlendes Ergebnis aussaehe.
    nur_intern = V1A.O.RunContext(
        run_id="ar-n", task_id="at-n", scope=S.SCOPE_RESEARCH,
        ledger=V1A.O.BU.BudgetLedger(budget=V1A.O.BU.DEFAULTS["research"]))
    nur_intern.context_notes.append("[core] Ein geplanter Schritt war unvollstaendig")
    require_equal(orch._summarise(nur_intern), "Der Lauf ist durch.")


def t_r_the_done_sentence_has_exactly_one_period():
    """Der gebundene Handlungstext endet oft selbst mit einem Punkt.

    Gemessen: „Erledigt: … schreiben..". Zwei Punkte hat niemand geschrieben.
    """
    _erledigt_satz = V1A.O._erledigt_satz
    einer = _erledigt_satz(["Eine Notiz mit dem Text 'x' schreiben."])
    require(not einer.endswith(".."), f"doppelter Punkt: {einer}")
    require(einer.endswith("schreiben."), einer)
    ohne = _erledigt_satz(["Die Notiz anlegen"])
    require_equal(ohne, "Erledigt: Die Notiz anlegen.")




def t_r_the_placeholder_never_precedes_a_real_result():
    """„Der Lauf ist durch." ist ein PLATZHALTER, kein Satzanfang.

    Gemessen an der Live-Abnahme: die Meldung lautete „Der Lauf ist durch.
    Erledigt: …". Der Platzhalter steht fuer „nichts zu berichten" — vor einem
    Ergebnis ist er eine Verneinung dessen, was danach kommt.
    """
    import ast, inspect
    quelle = inspect.getsource(V1A.O.Orchestrator._do_finish_check) \
        if hasattr(V1A.O.Orchestrator, "_do_finish_check") else ""
    if not quelle:
        # Der Zweig liegt in `_assess`/`_do_verify` — wir pruefen das VERHALTEN
        # ueber die beiden Bausteine statt ueber den Fundort.
        pass
    orch = V1A._orch(ledger=V1A._ledger())
    require_equal(orch.NO_NOTES, "Der Lauf ist durch.")
    # Der Zusammenbau selbst, an der Quelle abgelesen: der Platzhalter darf
    # nicht mit `f"{summary} ..."` vorangestellt werden.
    zusammenbau = inspect.getsource(V1A.O.Orchestrator)
    stelle = zusammenbau[zusammenbau.index("getan = _erledigt_satz"):][:300]
    require("summary == self.NO_NOTES" in stelle,
            f"der Platzhalter wird nicht unterdrueckt: {stelle[:160]}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
