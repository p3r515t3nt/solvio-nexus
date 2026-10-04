"""Die Zustandsmaschine — und die Stellen, an denen ein Modell nichts entscheidet.

Was hier geprueft wird, ist nicht „der Lauf laeuft durch", sondern die Kanten,
an denen Autoritaet entstehen koennte und nicht darf:

* Planvalidierung: unbekannte Schrittart, gesperrte Faehigkeit, fremdes Profil,
  Autoritaetsfeld im Argument, Builder in einem Rechercheauftrag → abgelehnt;
* Scope-Trennung ist strukturell, nicht deklarativ;
* parkende Laeufe belegen keinen Laufzeit-Slot;
* freigabepflichtige Schritte sind global serialisiert;
* eine Ablehnung ist endgueltig — es wird KEIN anderer Weg gesucht;
* die Schleifenbremse und die Budgets enden ehrlich;
* der Neustart-Abgleich verbucht nichts still als Erfolg.

Alles laeuft ohne Netz, ohne Anbieter und ohne Unterprozess: der Planer und der
Spezialist sind Fakes. Das ist Absicht — die Frage ist die Mechanik des Cores,
nicht die Laune eines Modells.
Der lokale Hermes-Seam verwendet dabei den echten Umschlagadapter und das
Kostenclaim-Tor mit `free_local`-Beleg. Die native Hermes-Abo-Route folgt in N3.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-orch-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import budget as BU  # noqa: E402
from solvio.agent_runtime import orchestrator as O  # noqa: E402
from solvio.agent_runtime import planner as PL  # noqa: E402
from solvio.agent_runtime import specialists as SP  # noqa: E402
from solvio.agent_runtime import steps as ST  # noqa: E402
from solvio.agent_runtime import store as S  # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome as OUT  # noqa: E402
from solvio.capabilities.envelope import CapabilityResult  # noqa: E402
from solvio.specialists.result import SpecialistResult  # noqa: E402
from _local_hermes_cost import install_local_hermes_cost  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _ledger() -> S.AgentRunLedger:
    folder = tempfile.mkdtemp(prefix="solvio-orch-db-")
    return S.AgentRunLedger(os.path.join(folder, "agent_runs.sqlite3"))


class FakePlanner:
    """Liefert einen festen Plan. Zaehlt, wie oft er gerufen wurde — die
    Aufrufinvariante ist eine eigene Zusicherung."""

    def __init__(self, steps=None, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self._steps = steps if steps is not None else [
            PL.PlannedStep(kind="specialist", profile="investigator/codex",
                           instruction="Pruefe X")]

    async def plan(self, *, goal, scope, allowed_profiles, known_capabilities,
                   ledger, run_id, context="", event_ordinal=0, capability_contracts=None):
        self.calls += 1
        ledger.check_planner()
        ledger.note_planner_call()
        if self.fail:
            raise PL.PlanInvalid("unusable")
        return PL.Plan(goal=goal, steps=tuple(self._steps)), PL.PlannerCall(True)


class FakeRouter:
    """Gibt vorbereitete Umschlaege zurueck und merkt sich die Stempel."""

    def __init__(self, outcomes=None) -> None:
        self.calls = []
        self._outcomes = list(outcomes or [])

    def names(self):
        return ["ha_light_set", "calendar_create_event"]

    async def execute(self, name, arguments=None, **kw):
        self.calls.append({"name": name, "arguments": arguments, **kw})
        if self._outcomes:
            return self._outcomes.pop(0)
        return CapabilityResult(OUT.SUCCESS, "c-1", name, human_message="ok")


class FakeControlPlane:
    def __init__(self, rows=None) -> None:
        self.store = self
        self._rows = rows or {}

    async def get_request(self, approval_id):
        return self._rows.get(approval_id)


def _orch(*, fixture_file=__file__, **kw) -> O.Orchestrator:
    kw.setdefault("ledger", _ledger())
    kw.setdefault("planner", FakePlanner())
    kw.setdefault("router", FakeRouter())
    return install_local_hermes_cost(O.Orchestrator(**kw), fixture_file=fixture_file)


class FakeResearcher:
    """Der Hermes-Seam, ohne Hermes. Dieselben zwei Methoden, die auch die
    Sprachseite ruft — mehr braucht der Adapter nicht."""

    #: Objective Execution V1A / FIX 1: die Zusammenfassung war 31 Zeichen lang.
    #: Das genuegte, solange am PLANENDE niemand nach Zielerfuellung fragte —
    #: seit FIX 1 fragt dort dieselbe Regel wie an der Abkuerzung, und
    #: `MIN_ANSWER` (40) trennt dort eine Antwort von einem Anfang. Der
    #: Gegenstand dieser Attrappe ist der ADAPTERWEG, nicht die Antwortlaenge;
    #: der echte Hermes liefert Absaetze. Geaendert wurde deshalb die Attrappe
    #: und nicht die Schranke — festgehalten, weil eine stillschweigend
    #: angepasste Zusicherung schlimmer waere als eine rote.
    ANTWORT = ("Das Projekt braucht Python 3.13; aeltere Versionen scheitern "
               "an der Typsyntax in den Vertraegen.")

    def __init__(self, running_first: bool = False) -> None:
        self.calls = []
        self._running_first = running_first

    async def research(self, arguments):
        self.calls.append(arguments)
        if self._running_first:
            return {"task_id": "dt-1", "status": "running"}
        return {"zusammenfassung": self.ANTWORT,
                "quellen": ["pyproject.toml"], "offene_fragen": []}

    async def status(self, arguments):
        return {"zusammenfassung": self.ANTWORT,
                "quellen": ["pyproject.toml"], "offene_fragen": []}


def _fake_specialist(ok: bool = True, findings=("Befund A",), quota: bool = False,
                    stderr_note: str = ""):
    async def _run_specialist(request, *, invocation_factory=None, researcher=None, on_event=None):
        return SP.SpecialistRun(result=SpecialistResult(
            role="scout", provider="codex", question=request.objective,
            ok=ok, reason="" if ok else "nonzero_exit",
            findings=list(findings), recommended_path="Weg A" if ok else ""),
            quota=quota, stderr_note=stderr_note)
    return _run_specialist


# =====================================================================
# Erzeugung und Herkunft
# =====================================================================

def t_creation_refuses_every_origin_outside_the_three():
    """Die zweite, unabhaengige Schranke. `BACKGROUND_AUTOMATION` fehlt
    ABSICHTLICH — das ist die Rekursionssperre: ein Lauf kann keinen Lauf
    gebaeren, denn seine Herkunft ist genau die."""
    orch = _orch()
    for origin in ("background_automation", "external_untrusted", "unspecified", ""):
        error = require_raises(O.CreationRefused, orch.create_task,
                               objective="Finde etwas heraus", scope="research",
                               origin=origin, principal="local-owner",
                               message=f"{origin} durfte eine Aufgabe anlegen")
        require("origin" in error.reason, f"falscher Grund: {error.reason}")
    for origin in ("trusted_interactive_app", "room_voice", "local_owner"):
        task, run = orch.create_task(objective="Finde etwas heraus", scope="research",
                                     origin=origin, principal="local-owner")
        require(task.task_id.startswith("at-"), "keine Aufgabe angelegt")


def t_a_build_task_is_refused_when_no_builder_is_released():
    """Ehrliche Nichtfaehigkeit statt eines Laufs, der spaeter scheitert."""
    orch = _orch()
    saved = dict(SP.BLOCKED_PROFILES)
    SP.BLOCKED_PROFILES.update({key: "test" for key, spec in SP.PROFILES.items()
                                if spec.mode == SP.BUILDER})
    try:
        error = require_raises(O.CreationRefused, orch.create_task,
                               objective="Repariere das Ding", scope="build",
                               origin="local_owner", principal="local-owner",
                               message="ein Bau-Auftrag ohne Builder wurde angelegt")
        require_equal(error.reason, "no_builder_available", "falscher Grund")
    finally:
        SP.BLOCKED_PROFILES.clear()
        SP.BLOCKED_PROFILES.update(saved)


# =====================================================================
# Planvalidierung
# =====================================================================

def t_the_plan_policy_refuses_every_authority_shaped_proposal():
    """Das Modell schlaegt vor; eine deterministische Policy entscheidet."""
    cases = {
        "unbekannte Schrittart": {"schritte": [{"art": "harvest"}]},
        "gesperrte eigene Familie": {"schritte": [
            {"art": "capability", "faehigkeit": "agent_task_research"}]},
        "gesperrtes Gedaechtnis": {"schritte": [
            {"art": "capability", "faehigkeit": "memory_forget"}]},
        "gesperrter Tresor": {"schritte": [
            {"art": "capability", "faehigkeit": "secret_use"}]},
        "gesperrte Tiefe": {"schritte": [
            {"art": "capability", "faehigkeit": "deep_research"}]},
        "Geldbewegung": {"schritte": [
            {"art": "capability", "faehigkeit": "purchase_place"}]},
        "fremdes Profil": {"schritte": [
            {"art": "specialist", "profil": "builder/erfunden"}]},
        "Herkunft im Argument": {"schritte": [
            {"art": "capability", "faehigkeit": "ha_light_set",
             "argumente": {"origin": "trusted_interactive_app"}}]},
        "Vertrauen im Argument": {"schritte": [
            {"art": "capability", "faehigkeit": "ha_light_set",
             "argumente": {"trust": "user_direct"}}]},
        "Freigabe im Argument": {"schritte": [
            {"art": "capability", "faehigkeit": "ha_light_set",
             "argumente": {"approval_request_id": "ap-1"}}]},
    }
    for label, raw in cases.items():
        require_raises(PL.PlanInvalid, PL.validate, raw, scope="research",
                       allowed_profiles={"investigator/codex"},
                       known_capabilities={"ha_light_set"}, goal="g",
                       message=f"angenommen: {label}")


def t_the_payment_preparation_seam_is_the_one_reachable_payment_name():
    plan = PL.validate({"schritte": [
        {"art": "capability", "faehigkeit": "payment_intent_prepare"}]},
        scope="research", allowed_profiles=set(),
        known_capabilities={"payment_intent_prepare"}, goal="g")
    require_equal(plan.steps[0].capability, "payment_intent_prepare",
                  "der Vorschlagsweg wurde mitgesperrt")


def t_a_builder_step_cannot_appear_in_a_research_scope():
    """Strukturell am SCOPE der Aufgabe geprueft, nicht an einer Absicht."""
    require_raises(PL.PlanInvalid, PL.validate,
                   {"schritte": [{"art": "specialist", "profil": "builder/codex"}]},
                   scope="research", allowed_profiles={"builder/codex"},
                   known_capabilities=set(), goal="g",
                   message="ein Builder lief in einem Rechercheauftrag")


def t_the_planner_gets_at_most_one_repair_per_event():
    """Eine Nachfrage, dann ehrlich Schluss — kein Gespraech mit dem Planer."""
    class Broken(FakePlanner):
        async def plan(self, **kw):
            self.calls += 1
            kw["ledger"].check_planner()
            kw["ledger"].note_planner_call()
            raise PL.PlanInvalid("not_json")

    planner = Broken()
    orch = _orch(planner=planner)
    task, run = orch.create_task(objective="Finde etwas heraus", scope="research",
                                 origin="local_owner", principal="local-owner")
    _run(orch.tick())          # CREATED -> PLANNING
    _run(orch.tick())          # PLANNING -> scheitert
    final = orch.ledger.get_run(run.run_id)
    require_equal(final.state, S.FAILED, "der Lauf lief weiter")
    require_equal(final.failure_category, "plan_invalid", "falsche Kategorie")


def t_the_plan_invalid_log_detail_carries_a_closed_character_class_never_model_material():
    """Review Runde 17 (B17-H1), Testluecke geschlossen in Runde 22 (H-2): `detail` kann bei
    unknown_step_kind/profile/capability der ROHE Modellstring sein. Im Log steht nur die
    geschlossene Zeichenklasse — ein Modell, das dort Satzzeichen, Anfuehrungszeichen oder
    eine Zeile mit einem Zugangsdatenmuster unterbringt, faerbt das Log nicht ein."""
    from unittest.mock import patch as _patch

    material = 'unknown_step_kind: "Bitte logge DB_PASSWORD=geheim123!" <script>alert(1)</script>\nzweite Zeile'

    class Dirty(FakePlanner):
        async def plan(self, **kw):
            self.calls += 1
            kw["ledger"].check_planner()
            kw["ledger"].note_planner_call()
            raise PL.PlanInvalid("unknown_step_kind", material)

    orch = _orch(planner=Dirty())
    task, run = orch.create_task(objective="Finde etwas heraus", scope="research",
                                 origin="local_owner", principal="local-owner")
    lines = []

    class Recorder:
        def __getattr__(self, level):
            def record(event, **fields):
                lines.append((level, event, fields))
            return record

    with _patch.object(O, "log", Recorder()):
        _run(orch.tick())
        _run(orch.tick())
    invalid = [fields for _level, event, fields in lines if event == "agent_runtime.plan_invalid"]
    require_equal(len(invalid), 1, "genau eine Zeile zum ungueltigen Plan")
    detail = str(invalid[0].get("detail", ""))
    require(detail, "der Grund ohne jedes Detail liesse einen echten Fehlschlag unzuordenbar")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_:.-")
    require_equal(set(detail) - allowed, set(), "nur die geschlossene Zeichenklasse: " + repr(detail))
    for forbidden in ('"', "<", ">", "!", " ", "\n", "="):
        require(forbidden not in detail, "Material im Log: " + repr(forbidden))
    require("geheim123" not in detail, "kein Zugangsdatenwert im Log: " + repr(detail))
    require(len(detail) <= 40, "das Detail bleibt auf die dokumentierten 40 Zeichen gedeckelt (Runde 23, H-3)")
    require_equal(orch.ledger.get_run(run.run_id).failure_category, "plan_invalid")

    # Zwei Schichten, die sich hier gegenseitig decken — deshalb je ein Fall, der NUR
    # die eine traegt (sonst ueberlebt jede Mutation die Probe, vgl. „Mutationen koennen
    # aequivalent sein"). (1) Ein Satz OHNE Zugangsdatengestalt: den kuerzt allein die
    # Ein-Wort-Regel. (2) Ein EINZELNES Wort mit Schluesselform: das faengt allein der Zaun.
    def _detail_for(raw):
        class One(FakePlanner):
            async def plan(self, **kw):
                self.calls += 1
                kw["ledger"].check_planner()
                kw["ledger"].note_planner_call()
                raise PL.PlanInvalid("unknown_step_kind", raw)

        local = _orch(planner=One())
        local.create_task(objective="Finde etwas heraus", scope="research",
                          origin="local_owner", principal="local-owner")
        seen = []

        class R:
            def __getattr__(self, level):
                def record(event, **fields):
                    seen.append((event, fields))
                return record

        with _patch.object(O, "log", R()):
            _run(local.tick())
            _run(local.tick())
        rows = [fields for event, fields in seen if event == "agent_runtime.plan_invalid"]
        require_equal(len(rows), 1, "genau eine Zeile")
        return str(rows[0].get("detail", ""))

    prose = _detail_for("unknown_step_kind Kundennummer 4711 Familienname Meier")
    require_equal(prose, "unknown_step_kind", "ein Modellsatz verliert alles bis auf sein erstes Wort")
    for leaked in ("4711", "Meier", "Kundennummer"):
        require(leaked not in prose, "Material aus dem Satz im Log: " + repr(prose))

    keyish = _detail_for("sk-ant-api03-CANARY-0123456789abcdefghijklmnop")
    require_equal(keyish, "material_redacted", "eine Schluesselform als EINZIGES Wort gehoert nicht ins Log")

    # Das Leitbeispiel von DEBT-0312 als EINZIGES Wort: der Zaun muss die ROHE Form sehen.
    # Nach der Zeichenklasse ist aus `DB_PASSWORD=geheim123` `DB_PASSWORDgeheim123` geworden
    # — und das erkannte der Zaun nicht mehr (Review Runde 24, R24-2).
    assigned = _detail_for("DB_PASSWORD=CANARY-geheim123")
    require_equal(assigned, "material_redacted", "eine Zuweisung an ein Zugangswort gehoert nicht ins Log")

    # (3) EIN Wort ohne Leerzeichen, aber voller Sonderzeichen: das traegt allein die
    # geschlossene Zeichenklasse — weder die Ein-Wort-Regel noch der Zaun greifen hier.
    # (4) Ein sehr langes EINZELNES Wort: das traegt allein die 40-Zeichen-Kappe — ohne sie
    # stuende der volle Bezeichner im Log (Review Runde 23, H-3: der Test prueft jetzt den
    # dokumentierten Wert, nicht die doppelte Laenge).
    long_word = "unknown_capability:kapazitaetsueberschreitung_in_einem_sehr_langen_bezeichner_ohne_ende"
    capped = _detail_for(long_word)
    require_equal(len(capped), 40, "die Kappe ist 40 Zeichen, nicht 80: " + repr(capped))
    require(long_word.startswith(capped), "gekuerzt wird von hinten, der Anfang bleibt zuordenbar")

    marked_up = _detail_for('kind:"<script>alert(1)</script>"')
    require_equal(set(marked_up) - allowed, set(), "die Zeichenklasse bleibt geschlossen: " + repr(marked_up))
    for forbidden in ('"', "<", ">", "(", ")"):
        require(forbidden not in marked_up, "Sonderzeichen im Log: " + repr(marked_up))


def t_capability_contracts_reach_the_real_planner_and_repair_missing_inputs():
    from solvio.capabilities.browser import SPECS
    class BrowserRouter(FakeRouter):
        def names(self):
            return list(SPECS)
        def spec(self, name):
            return SPECS[name]
    for capability, required in (("browser_open", "url"), ("browser_extract", "page_id")):
        for repaired in (True, False):
            calls = []
            async def transport(payload, **kw):
                calls.append(payload)
                if len(calls) == 2 and repaired:
                    proposal = {"schritte": [{"art": "specialist", "profil": "researcher/hermes",
                                              "auftrag": "Vergleiche drei Hotels mit Quellen."}]}
                else:
                    proposal = {"schritte": [{"art": "capability", "faehigkeit": capability,
                                              "argumente": {}}]}
                # This fixture isolates missing tool inputs, not missing
                # completion criteria: the repaired plan must satisfy both.
                proposal["anforderungen"] = {"auskunft": [
                    {"id": "a1", "text": "Vergleiche Hotels in Altona mit Quellen"}],
                    "handlungen": [], "unklar": [], "belege": {"mindestens": 1}}
                return {"ok": True, "text": json.dumps(proposal), "tokens": 100}
            planner = PL.Planner(transport=transport)
            router = BrowserRouter()
            orch = _orch(planner=planner, router=router, researcher=FakeResearcher())
            _, run = orch.create_task(objective="Vergleiche Hotels in Altona mit Quellen",
                                     scope="research", origin="local_owner", principal="owner")
            _run(orch.tick())
            _run(orch.tick())
            require_equal(len(calls), 2, "nicht die vorhandene einmalige Formatkorrektur")
            catalogue = json.loads(calls[0]["input"][1]["content"])["auswahl"]
            require_equal(catalogue["eingabevertraege"][capability], SPECS[capability].input_schema)
            require(f"{capability}:{required}" in calls[1]["input"][-1]["content"],
                    "die Nachfrage nennt nicht das fehlende Argument")
            current = orch.ledger.get_run(run.run_id)
            require_equal(current.state, S.RUNNING if repaired else S.FAILED)
            require_equal(current.plan_revision, 0, "Formatkorrektur verbrauchte eine Revision")
            require_equal(current.planner_calls, 2)
            require_equal(router.calls, [], "ungueltiger Schritt wurde ausgefuehrt")


def t_the_hard_planner_ceiling_is_six_calls_per_run():
    limits = BU.BudgetLedger(budget=BU.DEFAULTS["research"])
    for _ in range(BU.MAX_PLANNER_CALLS_PER_RUN):
        limits.check_planner()
        limits.note_planner_call()
    require_raises(BU.BudgetExhausted, limits.check_planner,
                   message="der siebte Planeraufruf lief durch")
    require_equal(BU.MAX_PLANNER_CALLS_PER_RUN, 6, "die Obergrenze hat sich verschoben")


# =====================================================================
# Laufzeit-Slots, Serialisierung, Schleifen
# =====================================================================

def t_a_parked_run_does_not_hold_a_runtime_slot():
    """Ein unbeantworteter Freigabedialog darf die ganze Laufzeit nicht verstopfen.

    Der erste Lauf parkt ECHT: mit wartendem Schritt und einer Anfrage, die der
    Kontrollweg als offen meldet. Ein blosses `transition(WAITING_APPROVAL)`
    ohne Schritt waere ein Lauf, den der Orchestrator zu Recht wieder anwirft —
    und der Test pruefte dann etwas anderes, als er behauptet.
    """
    import time as _t
    ledger = _ledger()
    approval = "ap-parked"
    control = FakeControlPlane({approval: {"state": "PENDING",
                                           "expires_at": _t.time() + 600}})
    orch = _orch(ledger=ledger, control_plane=control)
    _task, first = orch.create_task(objective="Auftrag eins hier", scope="research",
                                    origin="local_owner", principal="o")
    ledger.transition(first.run_id, S.PLANNING)
    ledger.transition(first.run_id, S.RUNNING)
    step = ledger.create_step(run_id=first.run_id, seq=1, kind="capability",
                              capability="ha_light_set")
    ledger.update_step(step.step_id, state="waiting", approval_id=approval)
    ledger.transition(first.run_id, S.WAITING_APPROVAL)
    require_equal(len(ledger.active_runs()), 0, "ein parkender Lauf belegt einen Slot")

    _task2, second = orch.create_task(objective="Auftrag zwei hier", scope="research",
                                      origin="local_owner", principal="o")
    _run(orch.tick())
    require_equal(ledger.get_run(first.run_id).state, S.WAITING_APPROVAL,
                  "der parkende Lauf wurde losgetreten")
    require(ledger.get_run(second.run_id).state != S.CREATED,
            "der zweite Lauf kam trotz freiem Slot nicht los")


def t_approval_steps_are_serialised_across_runs():
    """Zwei Laeufe koennen einander strukturell keine Freigabe kannibalisieren."""
    queue = ST.ApprovalQueue()
    require(queue.acquire("ar-A"), "A bekommt den Platz nicht")
    require(not queue.acquire("ar-B"), "B bekam den Platz, waehrend A ihn hielt")
    queue.release("ar-B")
    require(not queue.acquire("ar-B"), "eine fremde Freigabe hat den Platz geloest")
    queue.release("ar-A")
    require(queue.acquire("ar-B"), "nach der Freigabe kam B nicht dran")


def t_the_third_equivalent_attempt_is_refused():
    limits = BU.BudgetLedger(budget=BU.DEFAULTS["research"])
    limits.guard_attempt("specialist", "investigator/codex", "Finde X heraus")
    limits.guard_attempt("specialist", "investigator/codex", "finde  x   heraus!!")
    error = require_raises(BU.BudgetExhausted, limits.guard_attempt,
                           "specialist", "investigator/codex", "FINDE X HERAUS.",
                           message="der dritte gleichartige Versuch lief durch")
    require_equal(error.category, "loop_detected", "falsche Kategorie")


def t_a_low_value_result_counts_double():
    """Ein Spezialist, der dreimal dasselbe Nichts liefert, hat nicht drei
    Versuche gebraucht — er hat das Budget verbrannt."""
    limits = BU.BudgetLedger(budget=BU.DEFAULTS["research"])
    digest = limits.guard_attempt("specialist", "p", "tu etwas")
    limits.note_low_value(digest)
    require_raises(BU.BudgetExhausted, limits.guard_attempt, "specialist", "p",
                   "tu etwas", message="ein wertloser Versuch zaehlte nicht doppelt")


def t_budgets_end_honestly():
    limits = BU.BudgetLedger(budget=BU.Budget(max_steps=2,
                                              max_specialist_invocations=1,
                                              seconds=3600))
    limits.note_step(); limits.note_step()
    error = require_raises(BU.BudgetExhausted, limits.check_step,
                           message="das Schrittbudget greift nicht")
    require_equal(error.category, "budget_exhausted", "falsche Kategorie")
    limits.note_specialist()
    require_raises(BU.BudgetExhausted, limits.check_specialist,
                   message="das Spezialistenbudget greift nicht")


# =====================================================================
# Freigabe: gelesen, nicht geraten — und eine Ablehnung ist endgueltig
# =====================================================================

def t_a_denied_approval_ends_the_run_and_no_other_way_is_tried():
    ledger = _ledger()
    approval = "ap-denied"
    control = FakeControlPlane({approval: {"state": "DENIED", "expires_at": 0}})
    router = FakeRouter([CapabilityResult(
        OUT.APPROVAL_REQUIRED, "c-1", "ha_light_set", reason="awaiting_user_approval",
        data={"request_id": approval})])
    planner = FakePlanner([PL.PlannedStep(kind="capability",
                                          capability="ha_light_set",
                                          arguments={"entity": "licht"})])
    orch = _orch(ledger=ledger, router=router, planner=planner,
                 control_plane=control)
    _t, run = orch.create_task(objective="Schalte das Licht", scope="research",
                               origin="local_owner", principal="o")
    _run(orch.tick())      # -> PLANNING
    _run(orch.tick())      # -> RUNNING (Plan)
    _run(orch.tick())      # Faehigkeit -> WAITING_APPROVAL
    require_equal(ledger.get_run(run.run_id).state, S.WAITING_APPROVAL,
                  "der Lauf hat nicht geparkt")
    calls_before = len(router.calls)
    _run(orch.tick())      # Zustand lesen -> DENIED
    final = ledger.get_run(run.run_id)
    require_equal(final.state, S.FAILED, "eine Ablehnung beendete den Lauf nicht")
    require_equal(final.failure_category, "approval_denied", "falsche Kategorie")
    require_equal(len(router.calls), calls_before,
                  "nach der Ablehnung wurde ein weiterer Weg versucht")


def t_a_pending_approval_keeps_parking_and_never_blocks():
    ledger = _ledger()
    approval = "ap-pending"
    import time as _t
    control = FakeControlPlane({approval: {"state": "PENDING",
                                           "expires_at": _t.time() + 600}})
    router = FakeRouter([CapabilityResult(
        OUT.APPROVAL_REQUIRED, "c-1", "ha_light_set", reason="awaiting_user_approval",
        data={"request_id": approval})])
    planner = FakePlanner([PL.PlannedStep(kind="capability", capability="ha_light_set")])
    orch = _orch(ledger=ledger, router=router, planner=planner, control_plane=control)
    _t2, run = orch.create_task(objective="Schalte das Licht", scope="research",
                                origin="local_owner", principal="o")
    for _ in range(4):
        _run(orch.tick())
    require_equal(ledger.get_run(run.run_id).state, S.WAITING_APPROVAL,
                  "ein offener Freigabewunsch hat den Lauf nicht geparkt")


def t_an_expired_row_that_still_reads_pending_is_treated_as_expired():
    """`get_request` laeuft den Verfall NICHT mit — wer das nicht selbst prueft,
    parkt fuer immer an einer Freigabe, die es nicht mehr gibt."""
    import time as _t
    require_equal(ST.classify_approval({"state": "PENDING",
                                        "expires_at": _t.time() - 1}), ST.EXPIRED,
                  "eine verfallene Zeile las sich als offen")


def t_an_unknown_request_is_expired_not_denied():
    """Nach einem Neustart verfallen alle offenen Anfragen; die Zeile kann fort
    sein. Das als Ablehnung zu lesen waere die falsche Endgueltigkeit."""
    require_equal(ST.classify_approval(None), ST.EXPIRED, "unbekannt wurde Ablehnung")


def t_a_policy_denial_becomes_a_user_boundary_not_a_failure():
    """`BACKGROUND × VERY_CRITICAL = DENY` — der Lauf bittet, statt zu scheitern."""
    ledger = _ledger()
    router = FakeRouter([CapabilityResult(
        OUT.REJECTED_BY_POLICY, "c-1", "purchase_place", reason="policy_denied")])
    planner = FakePlanner([PL.PlannedStep(kind="capability",
                                          capability="payment_intent_prepare")])
    orch = _orch(ledger=ledger, router=router, planner=planner)
    _t, run = orch.create_task(objective="Kaufe die Sache", scope="research",
                               origin="local_owner", principal="o")
    for _ in range(3):
        _run(orch.tick())
    final = ledger.get_run(run.run_id)
    require_equal(final.state, S.WAITING_USER, "aus der Verweigerung wurde keine Grenze")
    require(final.boundary, "die Grenze wurde nicht aufgezeichnet")


# =====================================================================
# Stempel
# =====================================================================

def t_every_capability_call_carries_the_same_honest_stamps():
    ledger = _ledger()
    router = FakeRouter()
    planner = FakePlanner([PL.PlannedStep(kind="capability", capability="ha_light_set",
                                          arguments={"entity": "licht"})])
    orch = _orch(ledger=ledger, router=router, planner=planner)
    _t, run = orch.create_task(objective="Schalte das Licht", scope="research",
                               origin="trusted_interactive_app", principal="o")
    for _ in range(3):
        _run(orch.tick())
    require(router.calls, "es wurde keine Faehigkeit gerufen")
    call = router.calls[0]
    from solvio.capabilities.policy import OriginClass
    require_equal(call["origin"], OriginClass.BACKGROUND_AUTOMATION,
                  "die Vordergrund-Herkunft ist in den Lauf geblutet")
    require(call["principal"].startswith("agent:"), "falscher Principal")
    require(call["commanded"] is True, "commanded fehlt")
    require(call["trust"].user_authorized is True, "der Auftrag war nicht beauftragt")


def t_specialist_derived_arguments_are_untrusted_content():
    """Ein `SpecialistResult` traegt keine Je-Feld-Provenienz — also gibt es fuer
    seine Inhalte keine mildere Einstufung."""
    from solvio.capabilities.contract import ArgumentSource
    mapping = ST.provenance_map({"a": "specialist", "b": "planner", "c": "user"})
    require_equal(mapping["a"], ArgumentSource.UNTRUSTED_CONTENT, "Spezialist")
    require_equal(mapping["b"], ArgumentSource.MODEL_DERIVED, "Planer")
    require_equal(mapping["c"], ArgumentSource.USER_DIRECT, "Nutzer")


def t_a_research_scope_offers_only_profiles_that_need_no_repository():
    """Live gefunden: ein CLI-Ermittler bekam in einem `research`-Lauf einen
    LEEREN Ordner als cwd und scheiterte an einer Frage, die er nie beantworten
    konnte. Die Auswahl haengt jetzt am Arbeitsort, nicht an einer Hoffnung."""
    ledger = _ledger()
    orch = _orch(ledger=ledger, researcher=FakeResearcher())
    task, _run_ = orch.create_task(objective="Eine reine Recherche hier",
                                   scope="research", origin="local_owner",
                                   principal="o")
    allowed = orch._allowed_profiles(ledger.get_task(task.task_id))
    require_equal(sorted(allowed), ["researcher/hermes"],
                  f"ein Repo-Profil wurde fuer die Recherche angeboten: {allowed}")
    for key in allowed:
        require(not SP.profile(key).needs_workspace,
                f"{key} braucht ein Repo, bekommt aber keines")


def t_without_a_hermes_seam_the_research_scope_offers_nothing():
    """Ehrlich statt hilfsbereit: ohne Seam gibt es kein Rechercheprofil, und
    der Lauf endet an der Planpolitik statt an einem leeren Ordner."""
    ledger = _ledger()
    orch = _orch(ledger=ledger, researcher=None)
    task, _run_ = orch.create_task(objective="Eine reine Recherche hier",
                                   scope="research", origin="local_owner",
                                   principal="o")
    require_equal(sorted(orch._allowed_profiles(ledger.get_task(task.task_id))), [],
                  "ohne Hermes wurde trotzdem ein Rechercheprofil angeboten")


def t_the_hermes_adapter_never_goes_through_the_router():
    """`deep_*` bleibt auf der Sperrliste — der Adapter ruft den Seam direkt."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "agent_runtime", "specialists.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_hermes")
    # Ohne Docstring: der Adapter ERKLAERT, dass er TrustContext und Router
    # nicht anfasst. Eine Textsuche ueber die Rohfassung schluege genau an der
    # Zeile an, die es richtig macht — dieselbe Lehre wie beim Quellscan.
    body = ast.dump(ast.Module(body=[n for n in func.body
                                     if not (isinstance(n, ast.Expr)
                                             and isinstance(n.value, ast.Constant)
                                             and isinstance(n.value.value, str))],
                               type_ignores=[]))
    for forbidden in ("router", "execute", "TrustContext", "OriginClass"):
        require(forbidden not in body, f"der Adapter fasst {forbidden} an")
    from solvio.agent_runtime import authority as A
    require(A.is_blocked("deep_research"), "deep_research ist nicht mehr gesperrt")


def t_the_hermes_result_carries_no_authority_and_is_redacted():
    from solvio.specialists.result import CONTENT_TRUST
    spec = SP.profile("researcher/hermes")
    request = SP.SpecialistRequest(profile=spec.key, objective="frage", workdir="")
    poisoned = {"zusammenfassung": 'sk-ant-api03-CANARY0000000000000000000000',
                "quellen": ['"refresh_token": "CANARY0000000000000000000000"'],
                "offene_fragen": []}
    result = SP._hermes_result(spec, request, poisoned, 1.0)
    blob = str(result.as_dict()) + result.raw_excerpt
    require("sk-ant-api03-CANARY" not in blob, "ein Schluessel ueberlebte den Adapter")
    require("CANARY0000000000000000000000" not in blob, "ein Token ueberlebte")
    require_equal(result.as_dict()["content_trust"], CONTENT_TRUST,
                  "das Ergebnis gilt nicht als fremd")
    for field in ("approved", "risk", "trust", "origin"):
        require(not hasattr(result, field), f"Autoritaetsfeld {field}")


def t_the_deep_state_mirror_is_complete_and_matches_its_source():
    """Live gefunden: der Adapter kannte nur `running` und brach bei `queued`
    sofort ab — das Ergebnis war leer, und der Schritt scheiterte an einer
    Recherche, die noch nicht angefangen hatte.

    Der Test verlangt VOLLSTAENDIGKEIT: jeder Zustand des Seams ist entweder
    wartend, terminal-ohne-Ergebnis oder Erfolg. Ein neuer Zustand, den niemand
    einordnet, faellt hier auf statt still zu `empty_result` zu werden.
    """
    from solvio.contracts.deep_runtime import DeepTaskStatus
    known = {m.value for m in DeepTaskStatus}
    classified = SP.PENDING_DEEP_STATES | SP.FAILED_DEEP_STATES | {"succeeded"}
    require_equal(sorted(known - classified), [],
                  f"unklassifizierte Deep-Zustaende: {known - classified}")
    require_equal(sorted(classified - known), [],
                  f"erfundene Deep-Zustaende: {classified - known}")


def t_a_queued_research_is_awaited_not_abandoned():
    class Slow:
        def __init__(self):
            self.polls = 0
        async def research(self, arguments):
            return {"task_id": "dt-9", "status": "queued"}
        async def status(self, arguments):
            self.polls += 1
            if self.polls < 2:
                return {"task_id": "dt-9", "status": "running"}
            return {"zusammenfassung": "fertig", "quellen": ["q"], "offene_fragen": []}

    seam = Slow()
    saved = SP.PENDING_DEEP_STATES
    request = SP.SpecialistRequest(profile="researcher/hermes",
                                   objective="frage", workdir="")
    outcome = _run(SP.run_hermes(request, seam))
    require(outcome.result.ok, f"eine wartende Recherche wurde aufgegeben: {outcome.result.reason}")
    require_equal(outcome.result.findings, ["fertig"], "das Ergebnis fehlt")
    require(seam.polls >= 2, "es wurde nicht gepollt")


def t_a_failed_research_gets_an_honest_reason_not_empty_result():
    for state in sorted(SP.FAILED_DEEP_STATES):
        class Dead:
            async def research(self, arguments):
                return {"task_id": "dt-x", "status": state}
            async def status(self, arguments):
                return {"task_id": "dt-x", "status": state}
        request = SP.SpecialistRequest(profile="researcher/hermes",
                                       objective="frage", workdir="")
        outcome = _run(SP.run_hermes(request, Dead()))
        require(not outcome.result.ok, f"{state} galt als Erfolg")
        require(state in outcome.result.reason,
                f"{state} wurde zu '{outcome.result.reason}' verwischt")


def t_a_replan_does_not_collide_with_the_steps_of_the_first_plan():
    """Live gefunden, und von keiner Suite vorher gesehen.

    Der Nachplan faengt bei Schritt 1 wieder an. `UNIQUE(run_id, seq, attempt)`
    haelt das zu Recht auf — aber der Orchestrator zaehlte den Versuch nicht
    mit, und jeder Lauf mit Nachplanung starb an einem `IntegrityError`, den
    der generische Fang zu „unerwartet gescheitert" verwischte.

    Der Test erzwingt genau die Kollision: derselbe Plan zweimal, mit einem
    Schritt, der beim ersten Mal scheitert.
    """
    ledger = _ledger()

    class VaryingPlanner(FakePlanner):
        """Jeder Plan fragt etwas ANDERES — sonst greift die Schleifenbremse
        vor der Kollision, und der Test prueft die falsche Grenze."""

        async def plan(self, **kw):
            self.calls += 1
            kw["ledger"].check_planner()
            kw["ledger"].note_planner_call()
            step = PL.PlannedStep(kind="specialist", profile="researcher/hermes",
                                  instruction=f"Frage Nummer {self.calls}")
            return PL.Plan(goal=kw["goal"], steps=(step,)), PL.PlannerCall(True)

    planner = VaryingPlanner()

    class AlwaysEmpty:
        async def research(self, arguments):
            return {"zusammenfassung": "", "quellen": [], "offene_fragen": []}
        async def status(self, arguments):
            return {}

    orch = _orch(ledger=ledger, planner=planner, researcher=AlwaysEmpty())
    _t, run = orch.create_task(objective="Ein Auftrag mit Nachplanung",
                               scope="research", origin="local_owner", principal="o")
    for _ in range(10):
        _run(orch.tick())

    final = ledger.get_run(run.run_id)
    require("IntegrityError" not in (final.result_summary or ""),
            f"die Schrittnummern kollidierten: {final.result_summary}")
    # Welche Grenze zuerst greift, ist nicht der Gegenstand: bei dreimal
    # derselben Frage ist es die Schleifenbremse, bei mehr Vielfalt die
    # Revisionsgrenze. Beides ist ein EHRLICHES Ende — ein IntegrityError waere
    # keines.
    require(final.failure_category in ("loop_detected", "budget_exhausted"),
            f"der Lauf endete nicht an einer Grenze: "
            f"{final.failure_category} / {final.result_summary}")

    # Und die Schritte des zweiten Plans stehen als eigener VERSUCH im Buch.
    steps = ledger.steps_for_run(run.run_id)
    attempts = sorted({s.attempt for s in steps if s.kind == "specialist"})
    require(len(attempts) >= 2,
            f"die Nachplanung hat keinen eigenen Versuch bekommen: {attempts}")


def t_an_unexpected_failure_names_its_kind_in_the_book():
    """„Unerwartet gescheitert" allein zwingt dazu, jeden Fehlschlag im Log zu
    suchen. Der TYP gehoert ins Buch — kategorisch, nie der Text."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "agent_runtime", "orchestrator.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_advance")
    handlers = [n for n in ast.walk(func) if isinstance(n, ast.ExceptHandler)]
    generic = [h for h in handlers
               if isinstance(h.type, ast.Name) and h.type.id == "Exception"]
    require(generic, "es gibt keinen generischen Fang mehr")
    body = ast.dump(generic[-1])
    require("__name__" in body, "der Ausnahmetyp wird nicht benannt")
    # Kategorisch heisst kategorisch: der TEXT der Ausnahme darf nicht ins Buch.
    # Geprueft an der Struktur, nicht am Dump-Text — `args` kommt in jedem
    # `ast.dump` eines Aufrufs vor und waere ein Fehlalarm.
    leaks = []
    for node in ast.walk(generic[-1]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "str" and node.args \
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "exc":
            leaks.append("str(exc)")
        if isinstance(node, ast.Attribute) and node.attr == "args" \
                and isinstance(node.value, ast.Name) and node.value.id == "exc":
            leaks.append("exc.args")
    require_equal(leaks, [], f"der Ausnahmetext landet im Buch: {leaks}")


def t_a_replan_does_not_attempt_a_self_transition():
    """Live gefunden: die Nachplanung wollte `RUNNING → RUNNING` und liess den
    ganzen Lauf mit „unerwartet gescheitert" enden. Eine Nachplanung ist ein
    EREIGNIS im Zustand RUNNING, kein Zustandswechsel."""
    ledger = _ledger()
    planner = FakePlanner([PL.PlannedStep(kind="specialist",
                                          profile="researcher/hermes",
                                          instruction="Pruefe X")])

    class Empty:
        async def research(self, arguments):
            return {"zusammenfassung": "", "quellen": [], "offene_fragen": []}
        async def status(self, arguments):
            return {}

    orch = _orch(ledger=ledger, planner=planner, researcher=Empty())
    _t, run = orch.create_task(objective="Eine Recherche, die nichts findet",
                               scope="research", origin="local_owner", principal="o")
    for _ in range(6):
        _run(orch.tick())
    final = ledger.get_run(run.run_id)
    require(final.failure_category != "capability_failed",
            f"der Replan warf eine unerwartete Ausnahme: {final.result_summary}")
    require(final.state in (S.FAILED, S.SUCCEEDED),
            f"der Lauf endete nicht sauber: {final.state}")


class FakeWorkspaces:
    """Der Arbeitsbereichs-Verwalter, ohne git."""

    def __init__(self, refuse: bool = False, empty: bool = False) -> None:
        self.empty = empty
        self.cloned, self.harvested, self.cleaned = [], [], []
        self.discarded: list[str] = []
        self.refuse = refuse

    def discard_incomplete(self, run_id):
        # Getrennt von `cleaned` gefuehrt: ein Rest, den niemand kennt,
        # wegzuraeumen ist etwas anderes, als einem gescheiterten Lauf seinen
        # Arbeitsbereich zu nehmen. Nur das Zweite darf nicht passieren.
        self.discarded.append(run_id)
        return False

    def clone(self, run_id, repo, slug="arbeit"):
        from solvio.agent_runtime.workspace import Workspace
        self.cloned.append((run_id, repo))
        return Workspace(run_id=run_id, path=f"/tmp/ws/{run_id}", repo=repo or "/repo",
                         branch=f"agent/{slug}-{run_id}", base="base0")

    def harvest(self, workspace):
        from solvio.agent_runtime.workspace import HarvestRefused, NothingToHarvest
        if self.empty:
            raise NothingToHarvest("no_change", workspace.branch)
        if self.refuse:
            raise HarvestRefused("credential_shaped_content", "name:auth.json")
        self.harvested.append(workspace.run_id)
        return f"refs/agents/{workspace.run_id}"

    def cleanup(self, run_id, force=False):
        self.cleaned.append(run_id); return "removed"

    def reconcile(self, ledger):
        return []


def t_a_build_run_gets_its_clone_before_it_plans():
    """Live gefunden: der Klon wurde NIE angelegt. Der Planer plante einen
    Builder, der Schritt-Executor fand keinen Arbeitsort und lehnte ab — richtig,
    aber der Arbeitsort haette da sein muessen.

    Der Klon entsteht VOR der Planung, damit der Planer nicht etwas vorschlaegt,
    das erst danach moeglich wird.
    """
    ledger = _ledger()
    spaces = FakeWorkspaces()
    orch = _orch(ledger=ledger, workspaces=spaces,
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Lege eine Datei an")]))
    _t, run = orch.create_task(objective="Ein Bauauftrag mit Arbeitskopie",
                               scope="build", origin="local_owner", principal="o")
    _run(orch.tick())                       # CREATED -> Klon -> PLANNING
    require(spaces.cloned, "es wurde kein Klon angelegt")
    require_equal(spaces.cloned[0][0], run.run_id, "falscher Lauf geklont")
    require(ledger.get_run(run.run_id).workspace_path,
            "der Arbeitsbereich steht nicht im Buch")
    require(any("Arbeitskopie" in e.summary for e in ledger.events_for_run(run.run_id)),
            "der Klon wurde nicht gebucht")


def t_a_research_run_gets_no_clone():
    """Ein Rechercheauftrag hat keinen Arbeitsort — und bekommt auch keinen."""
    ledger = _ledger()
    spaces = FakeWorkspaces()
    orch = _orch(ledger=ledger, workspaces=spaces, researcher=FakeResearcher())
    _t, run = orch.create_task(objective="Eine reine Recherche hier",
                               scope="research", origin="local_owner", principal="o")
    _run(orch.tick())
    require_equal(spaces.cloned, [], "ein Rechercheauftrag bekam einen Klon")


def t_a_successful_build_run_harvests_and_says_it_is_the_users_decision():
    ledger = _ledger()
    spaces = FakeWorkspaces()
    orch = _orch(ledger=ledger, workspaces=spaces,
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Lege eine Datei an")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist()
    try:
        _t, run = orch.create_task(objective="Ein Bauauftrag mit Ergebnis",
                                   scope="build", origin="local_owner", principal="o")
        for _ in range(6):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved
    final = ledger.get_run(run.run_id)
    require_equal(final.state, S.SUCCEEDED, f"der Lauf endete als {final.state}")
    require(spaces.harvested, "es wurde nichts geerntet")
    require(final.branch_ref.startswith("refs/agents/"),
            f"kein Ernte-Verweis im Buch: {final.branch_ref}")
    require("deine Entscheidung" in final.result_summary,
            f"die Produktgrenze fehlt in der Meldung: {final.result_summary}")
    kinds = [s.kind for s in ledger.steps_for_run(run.run_id)]
    require("harvest" in kinds, f"kein Ernte-Schritt im Buch: {kinds}")


def t_a_refused_harvest_does_not_pretend_there_is_a_result():
    """Verweigert die Ernte, endet der Lauf ohne Ergebnis — nicht mit einem
    halben."""
    ledger = _ledger()
    spaces = FakeWorkspaces(refuse=True)
    orch = _orch(ledger=ledger, workspaces=spaces,
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Lege eine Datei an")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist()
    try:
        _t, run = orch.create_task(objective="Ein Bauauftrag mit Diebstahl",
                                   scope="build", origin="local_owner", principal="o")
        for _ in range(6):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved
    final = ledger.get_run(run.run_id)
    require_equal(final.branch_ref, "", "eine verweigerte Ernte hinterliess einen Verweis")
    require("deine Entscheidung" not in final.result_summary,
            "der Lauf behauptet ein Ergebnis, das nicht geerntet wurde")
    harvest_steps = [s for s in ledger.steps_for_run(run.run_id) if s.kind == "harvest"]
    require(harvest_steps and harvest_steps[0].state == "failed",
            "die verweigerte Ernte steht nicht als Fehlschlag im Buch")


def t_a_failed_specialist_puts_its_reason_in_the_book():
    """`nonzero_exit` allein sagt nicht, WARUM — und der Arbeitsbereich, in dem
    man haette nachsehen koennen, war nach dem Fehlschlag aufgeraeumt. Live
    gelernt: der Grund gehoert ins Buch, redigiert und gekappt."""
    ledger = _ledger()
    orch = _orch(ledger=ledger, researcher=FakeResearcher(),
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="researcher/hermes",
                     instruction="Pruefe X")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist(
        ok=False, stderr_note="codex: model not available in this region")
    try:
        _t, run = orch.create_task(objective="Ein Auftrag der scheitert",
                                   scope="research", origin="local_owner", principal="o")
        for _ in range(4):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved
    steps = [s for s in ledger.steps_for_run(run.run_id) if s.kind == "specialist"]
    require(steps, "kein Spezialistenschritt im Buch")
    require("model not available" in steps[0].summary,
            f"der Grund fehlt im Buch: '{steps[0].summary}'")


def t_a_build_run_without_a_result_is_not_a_success():
    """Der unehrlichste Fehler dieses Milestones, auf der Ebene des Laufs.

    Live: der Builder legte die Datei an und committete sie nicht, die Ernte
    nahm den unveraenderten Zweig, der Lauf hiess „fertig", und SOLVIO meldete
    „das Ergebnis liegt bereit". Es lag nichts bereit — die Ernte-Ref zeigte auf
    denselben Commit, von dem der Klon ausging.

    Der Docstring von `_harvest` versprach das Richtige laengst („endet der Lauf
    ehrlich ohne Ergebnis statt mit einem halben"). Der Code tat es nicht.
    """
    ledger = _ledger()
    orch = _orch(ledger=ledger, workspaces=FakeWorkspaces(empty=True),
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Lege eine Datei an")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist(ok=True)
    try:
        _t, run = orch.create_task(objective="Ein Bauauftrag ohne Wirkung",
                                   scope="build", origin="local_owner", principal="o")
        for _ in range(8):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved

    ended = ledger.get_run(run.run_id)
    require_equal(ended.state, S.FAILED,
                  "ein Lauf ohne Arbeitsergebnis galt als Erfolg")
    require_equal(ended.failure_category, "no_result", "falscher Grund")
    require(not ended.branch_ref, "es steht doch ein Ergebnis im Buch")
    require("liegt" not in (ended.result_summary or ""),
            f"dem Nutzer wurde ein Ergebnis versprochen: {ended.result_summary!r}")


def t_a_withheld_result_is_named_differently_than_an_empty_one():
    """„Nichts getan" und „etwas zurueckgehalten" sind nicht dasselbe.

    Beide enden ohne Ergebnis, aber der Unterschied gehoert dem Nutzer: im
    einen Fall gibt es nichts, im anderen gibt es etwas, das nicht hinaus darf.
    """
    ledger = _ledger()
    orch = _orch(ledger=ledger, workspaces=FakeWorkspaces(refuse=True),
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Lege eine Datei an")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist(ok=True)
    try:
        _t, run = orch.create_task(objective="Ein Bauauftrag mit Fund",
                                   scope="build", origin="local_owner", principal="o")
        for _ in range(8):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved

    ended = ledger.get_run(run.run_id)
    require_equal(ended.state, S.FAILED, "der Lauf gelang trotz Fund")
    require_equal(ended.failure_category, "no_result", "falscher Grund")
    require("Zugang" in (ended.result_summary or ""),
            f"der Fund wurde nicht benannt: {ended.result_summary!r}")


def t_a_failed_build_run_keeps_its_workspace_for_inspection():
    """Ohne den Arbeitsbereich bleibt nur das Wort „gescheitert". Der
    Startabgleich raeumt ihn spaeter auf."""
    ledger = _ledger()
    spaces = FakeWorkspaces()
    orch = _orch(ledger=ledger, workspaces=spaces,
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="builder/codex",
                     instruction="Baue etwas")]))
    saved = SP.run_specialist
    SP.run_specialist = _fake_specialist(ok=False, stderr_note="irgendein Fehler")
    try:
        _t, run = orch.create_task(objective="Ein Bauauftrag der scheitert",
                                   scope="build", origin="local_owner", principal="o")
        for _ in range(6):
            _run(orch.tick())
    finally:
        SP.run_specialist = saved
    require_equal(ledger.get_run(run.run_id).state, S.FAILED, "der Lauf gelang doch")
    require_equal(spaces.cleaned, [],
                  "der Arbeitsbereich eines gescheiterten Laufs wurde entsorgt")


def t_a_run_that_cannot_end_is_not_retried_forever():
    """Die teuerste Live-Lehre dieses Milestones, als Test.

    Der Hergang war: die Arbeitskopie liess sich nicht anlegen, der Lauf wollte
    scheitern, die Uebergangstabelle hatte die Kante nicht, `_finish`
    verschluckte den Fehler — und weil der Lauf damit nicht-terminal blieb,
    versuchte der Takt es alle zwei Sekunden erneut. 297 identische
    Fehlschlaege, bis von Hand gestoppt wurde.

    Die Tabelle ist repariert. Dieser Test prueft die ZWEITE Sicherung, die
    unabhaengig davon greift: wenn ein Uebergang doch je blockiert ist, faehrt
    der Takt nicht zweimal gegen dieselbe Wand.
    """
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Lauf, der nicht enden darf",
                               scope="research", origin="local_owner", principal="o")

    # Die Kante wegnehmen, die es im Live-Fall nicht gab.
    saved = S.TRANSITIONS[S.CREATED]
    S.TRANSITIONS[S.CREATED] = frozenset({S.PLANNING})
    try:
        _run(orch._finish(run.run_id, S.FAILED, "plan_invalid", "geht nicht"))
        require(run.run_id in orch._unfinishable,
                "der Lauf wurde nicht festgehalten — der Takt wiederholt ihn")
        require_equal(ledger.get_run(run.run_id).state, S.CREATED,
                      "der Zustand kippte doch")

        before = list(orch._contexts)
        for _ in range(5):
            _run(orch.tick())
        require_equal(list(orch._contexts), before,
                      "der Takt hat den unbeendbaren Lauf doch wieder angefasst")
    finally:
        S.TRANSITIONS[S.CREATED] = saved


# =====================================================================
# Eine erteilte Freigabe muss den Auftrag auch starten
#
# Der schwerste Fund dieses Milestones, live gemessen: der Nutzer gab per
# Face ID frei, und nichts geschah. Die Anfrage lief auf EXPIRED, mit NULL
# Ausfuehrungsversuchen.
#
# Der Grund war eine Henne-Ei-Luecke. Ein Lauf, der auf eine Freigabe wartet,
# hat `_poll_approval` im Takt. Der Auftrag, der den Lauf erst ERZEUGT, hatte
# niemanden: die Anfragekennung stand im Umschlag des Werkzeugs und starb mit
# dem Gespraechszug.
# =====================================================================

def _pending(ledger, request_id="ap-start-1", origin="trusted_interactive_app",
             commanded=False):
    ledger.remember_pending_start(
        request_id=request_id, capability="agent_task_build",
        arguments={"objective": "eine Datei anlegen"},
        principal="local-owner", origin=origin, commanded=commanded)


def t_an_approved_start_actually_starts():
    """Die Reparatur des Fundes: freigegeben heisst ausgefuehrt."""
    ledger = _ledger()
    router = FakeRouter()
    control = FakeControlPlane({"ap-start-1": {
        "state": "APPROVED", "expires_at": time.time() + 300}})
    orch = _orch(ledger=ledger, router=router, control_plane=control)
    _pending(ledger)

    _run(orch.tick())

    aufrufe = [c for c in router.calls if c["name"] == "agent_task_build"]
    require_equal(len(aufrufe), 1,
                  f"der freigegebene Auftrag wurde nicht gestartet: {router.calls}")
    require_equal(aufrufe[0]["approval_request_id"], "ap-start-1",
                  "die Kennung wurde nicht wieder vorgelegt")
    require_equal(ledger.waiting_starts(), [],
                  "der Merkzettel blieb offen")


def t_the_repeat_carries_the_origin_it_was_approved_under():
    """Die Herkunft wird zurueckgespielt, nie neu bestimmt.

    Sie geht in den Freigabe-Digest ein. Was am Raummikrofon freigegeben wurde,
    darf nicht als etwas Vertrauteres wiederkommen — sonst waere der
    Wiederholer eine Stelle, an der Autoritaet entsteht.
    """
    from solvio.capabilities.policy import OriginClass

    for label in ("room_voice", "trusted_interactive_app", "local_owner"):
        ledger = _ledger()
        router = FakeRouter()
        control = FakeControlPlane({"ap-start-1": {
            "state": "APPROVED", "expires_at": time.time() + 300}})
        orch = _orch(ledger=ledger, router=router, control_plane=control)
        _pending(ledger, origin=label, commanded=False)

        _run(orch.tick())

        aufruf = [c for c in router.calls if c["name"] == "agent_task_build"][0]
        require_equal(aufruf["origin"], OriginClass(label),
                      f"{label} wurde nicht zurueckgespielt")
        require_equal(aufruf["commanded"], False,
                      "der Wiederholer hat sich `commanded` selbst gegeben")


def t_a_denied_start_is_not_started_and_not_retried():
    """Eine abgelehnte Freigabe ist endgueltig — und wird nicht umgangen."""
    for zustand in ("DENIED", "EXPIRED"):
        ledger = _ledger()
        router = FakeRouter()
        control = FakeControlPlane({"ap-start-1": {
            "state": zustand, "expires_at": time.time() + 300}})
        orch = _orch(ledger=ledger, router=router, control_plane=control)
        _pending(ledger)

        for _ in range(3):
            _run(orch.tick())

        require_equal([c for c in router.calls if c["name"] == "agent_task_build"], [],
                      f"{zustand} hat den Auftrag doch gestartet")
        require_equal(ledger.waiting_starts(), [],
                      f"{zustand} blieb als Merkzettel offen")


def t_a_still_pending_start_keeps_waiting_without_starting():
    """Solange nicht entschieden ist, geschieht nichts — und der Merkzettel
    bleibt liegen, damit die spaetere Freigabe noch greift."""
    ledger = _ledger()
    router = FakeRouter()
    control = FakeControlPlane({"ap-start-1": {
        "state": "PENDING", "expires_at": time.time() + 300}})
    orch = _orch(ledger=ledger, router=router, control_plane=control)
    _pending(ledger)

    for _ in range(3):
        _run(orch.tick())

    require_equal([c for c in router.calls if c["name"] == "agent_task_build"], [],
                  "ein unentschiedener Auftrag wurde gestartet")
    require_equal(len(ledger.waiting_starts()), 1,
                  "der Merkzettel wurde zu frueh geschlossen")


def t_an_approved_start_is_taken_exactly_once():
    """Genommen wird VOR dem Ausfuehren.

    Sonst fuehrte ein Absturz zwischen Lesen und Ausfuehren zu einem zweiten
    Versuch — und der Auftrag liefe doppelt.
    """
    ledger = _ledger()
    router = FakeRouter()
    control = FakeControlPlane({"ap-start-1": {
        "state": "APPROVED", "expires_at": time.time() + 300}})
    orch = _orch(ledger=ledger, router=router, control_plane=control)
    _pending(ledger)

    for _ in range(5):
        _run(orch.tick())

    require_equal(len([c for c in router.calls if c["name"] == "agent_task_build"]), 1,
                  "der Auftrag lief mehr als einmal")


def t_an_unreadable_control_plane_does_not_start_anything():
    """Ein Lesefehler ist kein JA. Fail-closed, wie ueberall hier."""
    class Kaputt:
        def __init__(self):
            self.store = self

        async def get_request(self, approval_id):
            raise RuntimeError("Kontrollweg weg")

    ledger = _ledger()
    router = FakeRouter()
    orch = _orch(ledger=ledger, router=router, control_plane=Kaputt())
    _pending(ledger)

    _run(orch.tick())

    require_equal([c for c in router.calls if c["name"] == "agent_task_build"], [],
                  "ein Lesefehler hat den Auftrag gestartet")
    require_equal(len(ledger.waiting_starts()), 1,
                  "ein Lesefehler hat den Merkzettel geschlossen")


def t_the_tool_writes_the_note_that_makes_the_resume_possible():
    """Die andere Haelfte der Kette — ohne Merkzettel greift der Wiederholer nie.

    Genau das war der Live-Defekt: die Anfragekennung stand im Umschlag des
    Werkzeugs und starb mit dem Gespraechszug. Der Nutzer gab frei, und es gab
    nichts mehr, das die Freigabe haette einloesen koennen.
    """
    from solvio.capabilities.policy import OriginClass
    from solvio.tools.agent_capability_tools import AgentCapabilityTool

    class Gate:
        origin = OriginClass.TRUSTED_INTERACTIVE_APP
        principal = "local-owner"
        commanded = False
        has_principal = True
        trust = None

        def context(self):
            return self

        def provenance_for(self, args):
            return {}

    ledger = _ledger()
    router = FakeRouter([CapabilityResult(
        OUT.APPROVAL_REQUIRED, "c-1", "agent_task_build",
        reason="awaiting_user_approval", data={"request_id": "ap-merk-1"})])
    tool = AgentCapabilityTool("agent_task_build", router, Gate(), ledger)

    ergebnis = _run(tool.run({"objective": "eine Datei anlegen"}))
    require(not ergebnis.success, "eine wartende Freigabe galt als Erfolg")

    wartend = ledger.waiting_starts()
    require_equal(len(wartend), 1, f"kein Merkzettel entstanden: {wartend}")
    require_equal(wartend[0]["request_id"], "ap-merk-1", "falsche Kennung")
    require_equal(wartend[0]["origin"], "trusted_interactive_app",
                  "die Herkunft wurde nicht festgehalten")
    require_equal(wartend[0]["commanded"], False,
                  "`commanded` wurde nicht festgehalten")
    require_equal(wartend[0]["arguments"], {"objective": "eine Datei anlegen"},
                  "die Argumente kamen veraendert an — der Digest passt dann nicht")


def t_a_successful_call_leaves_no_note_behind():
    """Nur eine WARTENDE Anfrage bekommt einen Merkzettel.

    Sonst sammelte sich bei jedem gelungenen Auftrag eine Zeile an, die der
    Takt spaeter erneut auszufuehren versuchte.
    """
    from solvio.capabilities.policy import OriginClass
    from solvio.tools.agent_capability_tools import AgentCapabilityTool

    class Gate:
        origin = OriginClass.TRUSTED_INTERACTIVE_APP
        principal = "local-owner"
        commanded = True
        has_principal = True
        trust = None

        def context(self):
            return self

        def provenance_for(self, args):
            return {}

    ledger = _ledger()
    router = FakeRouter()
    tool = AgentCapabilityTool("agent_task_build", router, ledger=ledger, gate=Gate())
    _run(tool.run({"objective": "etwas"}))
    require_equal(ledger.waiting_starts(), [],
                  "ein gelungener Aufruf hinterliess einen Merkzettel")


def t_the_note_is_taken_before_the_start_not_after():
    """Die Reihenfolge, direkt beobachtet.

    Genommen wird VOR dem Ausfuehren. Der Unterschied zeigt sich erst, wenn
    der Prozess MITTEN drin stirbt — dann laege die Zeile sonst wieder auf
    WAITING, und der naechste Start liefe denselben Auftrag ein zweites Mal.
    Ein harter Abbruch laesst sich hier nicht nachstellen, die Reihenfolge
    schon: wenn der Start beginnt, muss die Zeile bereits genommen sein.
    """
    ledger = _ledger()
    control = FakeControlPlane({"ap-start-1": {
        "state": "APPROVED", "expires_at": time.time() + 300}})
    orch = _orch(ledger=ledger, router=FakeRouter(), control_plane=control)
    _pending(ledger)

    gesehen = {}
    echt = orch._start_approved

    async def spion(eintrag, request_id):
        gesehen["offen_beim_start"] = [w["request_id"]
                                       for w in ledger.waiting_starts()]
        return await echt(eintrag, request_id)

    orch._start_approved = spion
    _run(orch.tick())

    require("offen_beim_start" in gesehen, "der Start wurde nie versucht")
    require_equal(gesehen["offen_beim_start"], [],
                  "beim Start lag die Zeile noch offen — ein Absturz genau hier "
                  "wuerde den Auftrag ein zweites Mal starten")


def t_a_crash_while_starting_does_not_leave_the_note_open():
    """Und ein Fehlschlag im Start laesst die Zeile ebenfalls nicht offen."""
    ledger = _ledger()
    control = FakeControlPlane({"ap-start-1": {
        "state": "APPROVED", "expires_at": time.time() + 300}})

    class Explodiert:
        def names(self):
            return []

        async def execute(self, *a, **kw):
            raise RuntimeError("mitten drin weg")

    orch = _orch(ledger=ledger, router=Explodiert(), control_plane=control)
    _pending(ledger)

    for _ in range(3):
        _run(orch.tick())

    require_equal(ledger.waiting_starts(), [],
                  "nach einem Fehlschlag laege der Auftrag wieder zum Start bereit")


def t_the_resume_path_can_only_repeat_the_two_start_capabilities():
    """Die Rekursionssperre bleibt — hier steht eine ENGERE an ihrer Stelle.

    `authority.guard` verbietet einem Lauf alles mit dem Praefix `agent_`.
    Dieser Weg ist kein Lauf, sondern loest eine Freigabe ein, die ein Mensch
    bestaetigt hat. Er darf deshalb nicht durch dieselbe Sperre — aber er darf
    auch nicht MEHR koennen: genau zwei Namen, und keine Praefixregel, die
    spaeter still etwas dazunimmt.
    """
    from solvio.agent_runtime import authority as A
    from solvio.capabilities.policy import OriginClass

    router = FakeRouter()
    for verboten in ("ha_light_set", "memory_remember", "secret_add",
                     "agent_run_cancel", "payment_intent_prepare", "deep_research"):
        require_raises(
            A.CapabilityBlocked, lambda n=verboten: _run(ST.start_approved_capability(
                router, name=n, arguments={}, request_id="ap-x",
                principal="p", origin=OriginClass.LOCAL_OWNER, commanded=True)),
            message=f"{verboten} liess sich ueber den Freigabeweg starten")
    require_equal(router.calls, [], "ein verbotener Name erreichte den Router")

    for erlaubt in sorted(ST.RESUMABLE_STARTS):
        _run(ST.start_approved_capability(
            router, name=erlaubt, arguments={}, request_id="ap-x",
            principal="p", origin=OriginClass.LOCAL_OWNER, commanded=True))
    require_equal(len(router.calls), len(ST.RESUMABLE_STARTS),
                  "eine Start-Faehigkeit kam nicht durch")


# =====================================================================
# Neustart
# =====================================================================

def t_restart_marks_non_terminal_runs_interrupted_and_books_nothing_as_success():
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein langer Auftrag", scope="research",
                               origin="local_owner", principal="o")
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)

    report = _run(orch.reconcile())
    require(run.run_id in report["interrupted"], "der Lauf wurde nicht abgeglichen")
    after = ledger.get_run(run.run_id)
    require_equal(after.state, S.INTERRUPTED, "falscher Zustand nach dem Neustart")
    require_equal(after.outcome, "", "der Lauf wurde still als Ausgang verbucht")


def t_reconciliation_is_idempotent():
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein langer Auftrag", scope="research",
                               origin="local_owner", principal="o")
    ledger.transition(run.run_id, S.PLANNING)
    _run(orch.reconcile())
    second = _run(orch.reconcile())
    require_equal(second["interrupted"], [], "der zweite Abgleich markierte erneut")
    require_equal(ledger.get_run(run.run_id).state, S.INTERRUPTED, "Zustand kippte")


def t_a_user_boundary_survives_a_restart():
    """Eine Grenze wartet auf einen Menschen — ein Neustart macht daraus keine
    Unterbrechung, sonst ginge die Bitte verloren."""
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Auftrag mit Grenze", scope="research",
                               origin="local_owner", principal="o")
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.WAITING_USER)
    _run(orch.reconcile())
    require_equal(ledger.get_run(run.run_id).state, S.WAITING_USER,
                  "die Nutzergrenze wurde vom Abgleich ueberschrieben")


def t_an_orphan_is_only_killed_when_all_three_identities_match():
    """Die PID-Lehre: eine PID ist kein Besitztitel. Bei Unsicherheit wird
    gemeldet, nicht getoetet."""
    require(not O.process_group_matches(0, 0.0, ""), "pgid 0 galt als Treffer")
    require(not O.process_group_matches(1, 0.0, ""), "pgid 1 galt als Treffer")
    require(not O.process_group_matches(999_999, 1.0, "/usr/bin/definitely-not"),
            "ein erfundener Prozess galt als Treffer")


def t_a_running_step_with_an_unmatched_child_is_reported_not_killed():
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Auftrag mit Kind", scope="research",
                               origin="local_owner", principal="o")
    ledger.transition(run.run_id, S.PLANNING)
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="specialist")
    ledger.update_step(step.step_id, state="running", child_pgid=999_999,
                       child_started_at=1.0, child_executable="/usr/bin/nope")
    report = _run(orch.reconcile())
    require(step.step_id in report["reported"], "der Waise wurde nicht gemeldet")
    require(step.step_id not in report["killed"], "ein fremder Prozess wurde getoetet")
    require_equal(ledger.get_step(step.step_id).state, "unknown",
                  "der Schritt wurde als etwas anderes verbucht")


# =====================================================================
# Abbruch und Wiederaufnahme
# =====================================================================

def t_an_uncertain_step_never_lets_a_run_succeed():
    """Der schwerste Fund der Live-Abnahme.

    Nach einem Neustart war ein Kindprozess nicht mehr eindeutig zuzuordnen —
    der Abgleich markierte den Schritt korrekt als `unknown` und toetete nichts.
    Danach lief der Lauf weiter und endete SUCCEEDED, mit dem Satz „Alle
    Schritte haben ein Ergebnis". Die Pruefung sah nur `failed`/`denied`.

    Die Architektur verbietet genau das woertlich: nichts wird nach einem
    Neustart still als erfolgreich verbucht.
    """
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Lauf mit ungewissem Ausgang",
                               scope="research", origin="local_owner", principal="o")
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                              capability="ha_light_set")
    ledger.update_step(step.step_id, state="unknown", finished=True,
                       summary="Der Ausgang ist ungewiss.")
    ledger.transition(run.run_id, S.VERIFYING)

    context = orch._contexts.get(run.run_id) or orch._rebuild_context(
        ledger.get_run(run.run_id))
    _run(orch._do_verify(ledger.get_run(run.run_id), context))

    final = ledger.get_run(run.run_id)
    require(final.state != S.SUCCEEDED,
            "ein Lauf mit ungewissem Schritt wurde als Erfolg verbucht")
    require_equal(final.state, S.FAILED, f"falscher Endzustand: {final.state}")
    require_equal(final.failure_category, "recovery_required",
                  f"falsche Kategorie: {final.failure_category}")
    require("nicht sicher" in final.result_summary,
            f"das Ergebnis beschoenigt: {final.result_summary}")


def t_only_settled_steps_let_a_run_succeed():
    """Erlaubnisliste statt Sperrliste: was nicht nachweislich erledigt ist,
    laesst den Lauf nicht gelingen — auch ein vergessener `running`-Schritt nicht."""
    for state in ("pending", "running", "waiting", "failed", "denied", "unknown"):
        ledger = _ledger()
        orch = _orch(ledger=ledger)
        _t, run = orch.create_task(objective=f"Ein Lauf im Zustand {state}",
                                   scope="research", origin="local_owner", principal="o")
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        step = ledger.create_step(run_id=run.run_id, seq=1, kind="specialist")
        ledger.update_step(step.step_id, state=state)
        ledger.transition(run.run_id, S.VERIFYING)
        context = orch._rebuild_context(ledger.get_run(run.run_id))
        _run(orch._do_verify(ledger.get_run(run.run_id), context))
        require(ledger.get_run(run.run_id).state != S.SUCCEEDED,
                f"ein Schritt im Zustand '{state}' liess den Lauf gelingen")
    # Und die Gegenprobe: erledigt heisst erledigt.
    #
    # Objective Execution V1A / FIX 1: seit dem Abschluss zusaetzlich nach der
    # ZIELERFUELLUNG gefragt wird, braucht diese Gegenprobe beides — einen
    # erledigten Schritt UND ein beurteilbares Ziel mit einem Ergebnis. Sonst
    # pruefte sie nicht mehr das Schritt-Tor (ihr Gegenstand), sondern faende
    # nur noch das Ziel-Tor davor. Gegenstand und Aufbau, nicht die Zusage.
    for state in ("succeeded", "skipped"):
        ledger = _ledger()
        orch = _orch(ledger=ledger)
        _t, run = orch.create_task(
            objective=f"Finde heraus, wie ein erledigter Lauf {state} endet",
            scope="research", origin="local_owner", principal="o")
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        step = ledger.create_step(run_id=run.run_id, seq=1, kind="specialist")
        ledger.update_step(step.step_id, state=state)
        ledger.transition(run.run_id, S.VERIFYING)
        context = orch._rebuild_context(ledger.get_run(run.run_id))
        context.findings = ["Er endet mit einem Ergebnis, und genau das ist "
                            "hier der Gegenstand der Pruefung."]
        _run(orch._do_verify(ledger.get_run(run.run_id), context))
        # Das Schritt-Tor ist der Gegenstand. Ein erledigter Schritt laesst den
        # Lauf hindurch; danach fehlt ihm der Erfuellungsvertrag.
        # `goal_unverified` heisst „durch das Schritt-Tor", `no_result` hiesse
        # „daran gescheitert" — nur das Zweite waere hier ein Fehler.
        require_equal(ledger.get_run(run.run_id).failure_category,
                      "goal_unverified",
                      f"'{state}' kam nicht durch das Schritt-Tor")


def t_cancel_is_terminal_and_honest():
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Auftrag zum Abbrechen", scope="research",
                               origin="local_owner", principal="o")
    require(_run(orch.cancel(run.run_id)), "der Abbruch griff nicht")
    final = ledger.get_run(run.run_id)
    require_equal(final.state, S.CANCELLED, "falscher Endzustand")
    require_equal(final.failure_category, "cancelled_by_user", "falsche Kategorie")
    require(not _run(orch.cancel(run.run_id)), "ein terminaler Lauf liess sich abbrechen")


def t_resume_only_works_at_a_user_boundary():
    ledger = _ledger()
    orch = _orch(ledger=ledger)
    _t, run = orch.create_task(objective="Ein Auftrag zum Fortsetzen", scope="research",
                               origin="local_owner", principal="o")
    require(not _run(orch.resume(run.run_id)),
            "ein Lauf ohne Grenze liess sich fortsetzen")
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.WAITING_USER)
    require(_run(orch.resume(run.run_id)), "die Wiederaufnahme griff nicht")
    require_equal(ledger.get_run(run.run_id).state, S.RUNNING, "falscher Zustand")


# =====================================================================
# Ein ganzer Rechercheauftrag
# =====================================================================

def t_a_research_run_reaches_a_terminal_state_with_a_ledger_trail():
    """Der ganze Weg — ueber den ECHTEN Adapter, nur mit einem Fake-Seam.

    `SP.run_hermes` bleibt echt: die Umschlagauswertung ist der Gegenstand.
    Die aeussere Anbieternaht verwendet die lokale Testfunktion hinter einem
    echten Kostenclaim; der produktive N2-Kapselweg bleibt gesperrt.
    """
    ledger = _ledger()
    orch = _orch(ledger=ledger, researcher=FakeResearcher(),
                 planner=FakePlanner([PL.PlannedStep(
                     kind="specialist", profile="researcher/hermes",
                     instruction="Welche Python-Version braucht das Projekt?")]))
    _t, run = orch.create_task(objective="Finde heraus, warum X klemmt",
                               scope="research", origin="room_voice",
                               principal="local-owner")
    for _ in range(6):
        _run(orch.tick())

    final = ledger.get_run(run.run_id)
    # Informationsvertrag: dieser Planer schlaegt keine Anforderungen vor, also
    # gibt es keinen Vertrag und keinen automatischen Abschluss. Der Lauf endet
    # ehrlich terminal mit seinem Ergebnis — und ohne Rueckfrage.
    require(final.terminal, f"der Lauf endete als {final.state}")
    require_equal(final.failure_category, "goal_unverified",
                  f"falscher Grund: {final.failure_category}")
    require(final.result_summary, "kein Ergebnis im Buch")
    kinds = [s.kind for s in ledger.steps_for_run(run.run_id)]
    require("specialist" in kinds and "verify" in kinds, f"Schritte fehlen: {kinds}")
    events = ledger.events_for_run(run.run_id)
    require(len(events) >= 4, "das Ereignisjournal ist zu duenn")
    require(not [e for e in events if e.kind == "boundary_opened"],
            "es wurde pauschal nach einer Owner-Entscheidung gefragt")
    require(any(e.kind == "notice_sent" for e in events), "es wurde nichts gemeldet")


def t_pending_registration_preserves_binding_and_never_reopens_claim():
    ledger = _ledger()
    _pending(ledger, origin="room_voice")
    _pending(ledger, origin="local_owner", commanded=True)
    require_equal(ledger.waiting_starts()[0]["origin"], "room_voice")
    require_equal(ledger.waiting_starts()[0]["commanded"], False)
    require(ledger.claim_pending_start("ap-start-1"))
    _pending(ledger)
    require_equal(ledger.waiting_starts(), [])
    require(not ledger.claim_pending_start("ap-start-1"))
    require(not ledger.claim_pending_start("nonexistent"))


def t_two_pollers_with_the_same_snapshot_execute_only_once():
    async def probe():
        ledger = _ledger()
        _pending(ledger)
        router = FakeRouter()
        both_read = asyncio.Event()

        class Control:
            def __init__(self):
                self.store = self
                self.readers = 0

            async def get_request(self, request_id):
                self.readers += 1
                if self.readers == 2:
                    both_read.set()
                await both_read.wait()
                return {"state": "APPROVED", "expires_at": time.time() + 300}

        control = Control()
        first = _orch(ledger=ledger, router=router, control_plane=control)
        second = _orch(ledger=S.AgentRunLedger(ledger.path), router=router,
                       control_plane=control)
        await asyncio.wait_for(asyncio.gather(first.tick(), second.tick()), 5)
        require_equal(len(router.calls), 1)

    _run(probe())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
