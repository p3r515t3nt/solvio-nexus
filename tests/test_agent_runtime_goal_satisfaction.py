"""Zielerfuellung, Planvalidierung, Replan-Sperre — der letzte Release-Blocker.

Der Lauf, aus dem diese Suite entstanden ist, lief am 2026-08-30 live: der
Kundschafter beantwortete die Frage nach dem Haushaltsstrompreis 2024 in 45
Sekunden vollstaendig, mit drei Quellen. Danach plante der Planer `browser_open`
OHNE `url`, der Router lehnte richtig ab, zwei Nachplanungen erzeugten denselben
Schritt, das Revisionsbudget war auf, und der ganze Lauf endete auf
`FAILED / budget_exhausted`. Der Posteingang meldete „Ich komme so nicht weiter"
mit `findings=[]` — waehrend die fertige Antwort im Journal lag.

Vier Dinge werden hier geprueft, und jedes einzeln:

* die Zielerfuellung beendet den Plan, wenn nichts mehr offen ist — und sie tut
  es NICHT, wenn das Ziel eine Wirkung verlangt oder Belege fehlen;
* ein Schritt ohne Pflichtangabe erreicht den Router gar nicht;
* derselbe strukturelle Mangel wird nach der Nachplanung nicht noch einmal
  durchlaufen;
* was erarbeitet wurde, steht in Meldung und Artefakt — auch wenn der Lauf
  scheitert.

Alles ohne Netz, ohne Anbieter, ohne Unterprozess. Das Schema von `browser_open`
wird aus dem ECHTEN Werkzeugvertrag gezogen: ein Test gegen eine erfundene
Vertragsform wuerde genau den Fehler nicht finden, um den es hier geht.
Der lokale Rechercheur geht durch den echten Hermes-Umschlagadapter und das
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
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-ziel-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import completion as CO  # noqa: E402
from solvio.agent_runtime import orchestrator as O  # noqa: E402
from solvio.agent_runtime import planner as PL  # noqa: E402
from solvio.agent_runtime import store as S  # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome as OUT  # noqa: E402
from solvio.capabilities.envelope import CapabilityResult  # noqa: E402
from solvio.specialists.result import SpecialistResult  # noqa: E402
from solvio.tools.browser_capability_tools import _SCHEMAS  # noqa: E402
from _local_hermes_cost import install_local_hermes_cost  # noqa: E402

#: Der ECHTE Vertrag der Faehigkeit, die live gescheitert ist.
BROWSER_OPEN_SCHEMA = _SCHEMAS["browser_open"]["parameters"]

#: Das Ziel des Live-Laufs, woertlich.
LIVE_GOAL = ("Finde heraus, wie hoch der durchschnittliche Haushaltsstrompreis "
             "in Deutschland im Jahr 2024 war, belege das mit zwei "
             "verlaesslichen Quellen und melde das Ergebnis, sobald es vorliegt.")

#: Die Antwort des Live-Laufs, gekuerzt — mit ihren drei Quellen.
LIVE_ANSWER = ("Fuer 2024 lag der durchschnittliche Haushaltsstrompreis in "
               "Deutschland bei rund 40 ct/kWh. Der BDEW weist 40,1 ct/kWh aus; "
               "Destatis nennt 41,02 ct/kWh fuer das 1. und 40,55 ct/kWh fuer "
               "das 2. Halbjahr 2024.")
LIVE_SOURCES = ["https://www.bdew.de/service/daten-und-grafiken/",
                "https://www.destatis.de/DE/Presse/Pressemitteilungen/2024/09/",
                "https://www.destatis.de/DE/Themen/Wirtschaft/Preise/"]


def _run(coro):
    return asyncio.run(coro)


def _budget():
    from solvio.agent_runtime import budget as BU
    return BU.BudgetLedger(budget=BU.DEFAULTS["research"])


def _ledger() -> S.AgentRunLedger:
    folder = tempfile.mkdtemp(prefix="solvio-ziel-db-")
    return S.AgentRunLedger(os.path.join(folder, "agent_runs.sqlite3"))


class Spec:
    """Nur das Feld, das der Orchestrator liest — mehr braucht die Pruefung
    nicht, und mehr zu erfinden waere eine zweite Vertragsform."""

    def __init__(self, input_schema: dict) -> None:
        self.input_schema = input_schema


class Router:
    """Kennt Vertraege UND liefert Umschlaege. Zaehlt jeden Aufruf mit — die
    Frage „hat der Router das ueberhaupt gesehen" ist der Kern von B."""

    def __init__(self, outcomes=None, specs=None) -> None:
        self.calls: list[dict] = []
        self._outcomes = list(outcomes or [])
        self._specs = dict(specs or {"browser_open": BROWSER_OPEN_SCHEMA})

    def names(self):
        return sorted(self._specs)

    def spec(self, name):
        schema = self._specs.get(name)
        return Spec(schema) if schema is not None else None

    async def execute(self, name, arguments=None, **kw):
        self.calls.append({"name": name, "arguments": arguments})
        if self._outcomes:
            return self._outcomes.pop(0)
        return CapabilityResult(OUT.SUCCESS, "c-1", name, human_message="ok")


class Proactive:
    """Der Posteingang, als Liste. `add_item` ist alles, was `notices` ruft."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    async def add_item(self, item):
        self.items.append(item)
        return True


class ControlPlane:
    """Der Lesepfad der Freigabeschicht, als eine Zeile. Nur `get_request` —
    mehr liest `read_approval_state` nicht."""

    def __init__(self, rows=None) -> None:
        self.store = self
        self._rows = dict(rows or {})

    async def get_request(self, approval_id):
        return self._rows.get(approval_id)


class Planner:
    """Liefert je Planungsereignis einen Plan aus einer festen Folge."""

    def __init__(self, plans) -> None:
        self.calls = 0
        self._plans = list(plans)
        #: Was dem Planer je Ereignis an Kontext mitgegeben wurde. Genau das ist
        #: die Frage bei C: erfaehrt er den GRUND oder nur den Fehlschlag?
        self.contexts: list[str] = []

    async def plan(self, *, goal, scope, allowed_profiles, known_capabilities,
                   ledger, run_id, context="", event_ordinal=0, capability_contracts=None):
        self.calls += 1
        self.contexts.append(context or "")
        ledger.check_planner()
        ledger.note_planner_call()
        steps = self._plans[min(self.calls - 1, len(self._plans) - 1)]
        return PL.Plan(goal=goal, steps=tuple(steps)), PL.PlannerCall(True)


class Researcher:
    """Der Hermes-Seam, ohne Hermes — dieselben zwei Methoden wie live."""

    def __init__(self, summary=LIVE_ANSWER, sources=None) -> None:
        self.calls: list[dict] = []
        self._summary = summary
        self._sources = list(LIVE_SOURCES if sources is None else sources)

    def _umschlag(self):
        """GENAU die Form, die `DeepCapabilities._settled` liefert.

        Die alten Attrappen antworteten flach — eine Form, die der echte Seam
        nie erzeugt. Deshalb sahen 2767 gruene Tests nicht, dass der Adapter
        eine Ebene zu hoch las, und zwei echte Laeufe endeten ohne Ergebnis.
        """
        return {"task_id": "dt-test", "status": "succeeded",
                "lage": "fertig", "abgeschlossen": True,
                "ergebnis": {"zusammenfassung": self._summary,
                             "quellen": list(self._sources),
                             "offene_fragen": []},
                "quellen": list(self._sources),
                "content_trust": "untrusted_executor"}

    async def research(self, arguments):
        self.calls.append(arguments)
        return self._umschlag()

    async def status(self, arguments):
        return self._umschlag()


_SCOUT = PL.PlannedStep(kind="specialist", profile="researcher/hermes",
                        instruction="Recherchiere den Strompreis 2024")
#: Genau der Schritt, der live dreimal entstand: die Faehigkeit ohne ihre
#: Pflichtangabe. Die Argumente wechseln absichtlich — live taten sie das auch,
#: und genau deshalb griff die vorhandene Schleifenbremse nicht.
def _browser_open(**arguments) -> PL.PlannedStep:
    return PL.PlannedStep(kind="capability", capability="browser_open",
                          instruction="Seite oeffnen", arguments=dict(arguments))


def _orch(*, plans, researcher=None, router=None, proactive=None, ledger=None,
          control_plane=None):
    orch = O.Orchestrator(
        ledger=ledger or _ledger(), planner=Planner(plans),
        router=router or Router(), proactive=proactive or Proactive(),
        control_plane=control_plane,
        researcher=researcher if researcher is not None else Researcher())
    return install_local_hermes_cost(orch, fixture_file=__file__)


def _drive(orch, objective=LIVE_GOAL, scope="research", ticks=14):
    _task, run = orch.create_task(objective=objective, scope=scope,
                                  origin="room_voice", principal="local-owner")
    for _ in range(ticks):
        _run(orch.tick())
    return orch.ledger.get_run(run.run_id)


# =====================================================================
# A — Zielerfuellung
# =====================================================================

def t_the_live_electricity_price_run_delivers_its_answer():
    """Der Lauf, der live scheiterte — mit demselben Plan, demselben Ziel und
    derselben Antwort.

    **Objective Execution V1A / FIX 2 hat die Zusage dieser Pruefung
    veraendert, und zwar bewusst.** Frueher endete der Lauf `SUCCEEDED`, weil
    die vorzeitige Vollendung den Plan kuerzte, sobald die Antwort lang genug
    war und genug Quellen trug. Genau diese Wortheuristik hat der Chief
    Architect am 5.9.2026 als Erfuellungsnachweis abgelehnt: sie liess sich mit
    einem Komma aushebeln.

    Was bleibt, ist der eigentliche Fund von damals — **die fertige Antwort
    erreicht den Menschen.** Was sich aendert: der Lauf kuerzt den Plan nicht
    mehr, laeuft in den strukturell ungueltigen Folgeschritt und endet ohne
    Erfolgsmeldung. Der Router wird dabei weiterhin nie angefasst.

    Das ist der gemessene Preis der Entscheidung und steht hier, damit er
    nicht als Nebensache durchgeht.
    """
    router = Router()
    proactive = Proactive()
    orch = _orch(plans=[[_SCOUT, _browser_open(ziel="strompreis"),
                         PL.PlannedStep(kind="verify")]],
                 router=router, proactive=proactive)
    final = _drive(orch)

    require(final.state != S.SUCCEEDED,
            "ein Rechercheziel wurde wieder als erfuellt verbucht")
    require_equal(router.calls, [], "der Router wurde ueberhaupt angefasst")
    letzte = proactive.items[-1]
    require(letzte["findings"], f"die fertige Antwort ging verloren: {letzte}")
    require(any("40" in f for f in letzte["findings"]),
            f"die Antwort kam nicht beim Menschen an: {letzte['findings']}")


def t_no_research_goal_has_a_fulfilment_contract_in_v1a():
    """Was von der Erfuellungspruefung uebrig ist — und warum.

    Diese Pruefung stand hier dreimal in anderer Gestalt: erst Antwortlaenge
    und Verbliste, dann eine Abdeckungsregel. Beide Male war es dieselbe Frage
    („kommt ein bestimmtes Wort vor?") und beide Male aushebelbar. Der Chief
    Architect hat die Gattung abgelehnt.

    Geblieben ist eine Entscheidung ohne Zieltext: fuer Recherche gibt es in
    V1A keinen Erfuellungsvertrag. Das Ergebnis bleibt verfuegbar — die
    Erfuellungsaussage faellt weg.
    """
    require(not CO.evaluate(scope="research").satisfied,
            "ein Rechercheziel hat wieder einen Erfuellungsvertrag")
    require_equal(CO.evaluate(scope="research").reason,
                  "no_supported_fulfilment_contract", "falscher Grund")
    import inspect
    require("goal" not in inspect.signature(CO.evaluate).parameters,
            "der Zieltext geht wieder in die Entscheidung ein")


def t_an_incomplete_result_still_allows_a_sensible_next_step():
    """Unvollstaendig heisst weiterarbeiten — nicht abkuerzen.

    Der Kundschafter liefert nur EINE Quelle, das Ziel verlangt zwei. Der
    geplante Folgeschritt ist gueltig und muss laufen.

    **Objective Execution V1A / FIX 1 hat die zweite Zusage dieser Pruefung
    umgedreht, und das war der Sinn der Sache.** Frueher stand hier
    `SUCCEEDED`: der Lauf endete erfolgreich, obwohl das Ziel ZWEI Quellen
    verlangte und EINE geliefert wurde. Genau diese Familie hat der Chief
    Architect am 5.9.2026 als F1 beanstandet — der regulaere Abschluss fragte
    nie nach Zielerfuellung. Er fragt jetzt, mit derselben Regel wie die
    Abkuerzung, und eine von zwei geforderten Quellen erfuellt nichts.

    Der eigentliche Gegenstand — der sinnvolle Folgeschritt LAEUFT — steht
    unveraendert in der ersten Zusicherung.
    """
    router = Router(specs={"notiz_ablegen": {"type": "object",
                                            "properties": {"text": {"type": "string"}},
                                            "required": ["text"]}})
    orch = _orch(plans=[[_SCOUT,
                         PL.PlannedStep(kind="capability", capability="notiz_ablegen",
                                        arguments={"text": "Zwischenstand"})]],
                 researcher=Researcher(sources=LIVE_SOURCES[:1]), router=router)
    final = _drive(orch)
    require_equal([c["name"] for c in router.calls], ["notiz_ablegen"],
                  f"der sinnvolle Folgeschritt lief nicht: {router.calls}")
    require(final.state != S.SUCCEEDED,
            "eine von zwei geforderten Quellen galt als Zielerfuellung")
    # Ohne gebundene Anforderungen (dieser Planer schlaegt keine vor) gibt es
    # keinen Vertrag — der Lauf endet ehrlich terminal, ohne Rueckfrage.
    require(final.terminal, f"der Lauf parkte: {final.state}")
    require_equal(final.failure_category, "goal_unverified",
                  f"falscher Grund: {final.failure_category}")
    require(final.result_summary and "40" in final.result_summary,
            f"das Teilergebnis ging verloren: {final.result_summary}")


def t_no_run_reports_work_it_did_not_do():
    """Kein `specialist_count=0`-Scheinerfolg.

    Frueher hing die Zusage an der vorzeitigen Vollendung; die ist seit FIX 2
    ohne Wirkung. Die Frage dahinter bleibt und wird jetzt direkt gestellt: was
    das Buch als geleistete Spezialistenarbeit ausweist, muss stattgefunden
    haben.
    """
    orch = _orch(plans=[[_SCOUT, _browser_open()]])
    final = _drive(orch)
    require(final.state != S.SUCCEEDED,
            "ein Rechercheziel wurde als erfuellt verbucht")
    require(final.specialist_count >= 1,
            f"das Buch weist keinen Spezialistenlauf aus: {final.specialist_count}")
    require(final.specialist_seconds >= 0.0, "die Spezialistenzeit fehlt")


# =====================================================================
# B — Planvalidierung
# =====================================================================

def t_a_capability_without_its_required_argument_never_reaches_the_router():
    """`browser_open` ohne `url` darf gar nicht erst zur Ausfuehrung gelangen.

    Das Ziel verlangt hier eine WIRKUNG, damit die Zielerfuellung nicht vorher
    greift — sonst pruefte der Test A statt B.
    """
    router = Router()
    orch = _orch(plans=[[_SCOUT, _browser_open(ziel="egal")],
                        [_SCOUT]],
                 router=router)
    final = _drive(orch, objective="Finde den Preis und schreib ihn auf.")
    require(not [c for c in router.calls if c["name"] == "browser_open"],
            f"der unvollstaendige Schritt erreichte den Router: {router.calls}")
    steps = [s for s in orch.ledger.steps_for_run(final.run_id)
             if s.capability == "browser_open"]
    require(steps, "der ungueltige Schritt wurde gar nicht gebucht")
    require(steps[0].outcome_reason.startswith("planner_invalid_step:"),
            f"der Mangel bekam keine eigene Kategorie: {steps[0].outcome_reason}")
    require("url" in steps[0].outcome_reason,
            f"die fehlende Pflichtangabe wird nicht benannt: {steps[0].outcome_reason}")


def t_the_validation_is_weaker_than_the_router_and_never_rejects_more():
    """Diese Pruefung ist eine vorgezogene Kopie der Router-Regel, keine zweite.

    Sie sieht NUR fehlende Pflichtargumente. Unbekannte Schluessel und falsche
    Typen bleiben Sache des Routers — sonst koennte hier etwas abgelehnt
    werden, das dort durchginge.
    """
    orch = _orch(plans=[[_SCOUT]])
    voll = PL.PlannedStep(kind="capability", capability="browser_open",
                          arguments={"url": "https://example.org", "extra": 1})
    require_equal(orch._structural_flaw(voll), "",
                  "ein unbekannter Schluessel wurde hier schon abgelehnt")
    fehlt = PL.PlannedStep(kind="capability", capability="browser_open",
                           arguments={})
    require_equal(orch._structural_flaw(fehlt), "missing_argument:url",
                  orch._structural_flaw(fehlt))


def t_without_a_reachable_contract_nothing_is_guessed():
    """Kennt der Router keinen Vertrag, wird nicht geraten — der Schritt laeuft
    wie bisher und der Router entscheidet."""
    orch = _orch(plans=[[_SCOUT]], router=Router(specs={}))
    schritt = PL.PlannedStep(kind="capability", capability="unbekannt",
                             arguments={})
    require_equal(orch._structural_flaw(schritt), "",
                  "ohne Vertrag wurde ein Mangel erfunden")


# =====================================================================
# C — Replan-Sperre
# =====================================================================

def t_the_same_invalid_step_is_not_executed_twice_after_a_replan():
    """Live dreimal derselbe Mangel mit wechselnden Argumenten.

    Die Schleifenbremse zaehlt einen Digest ueber die Argument-GESTALT und
    greift deshalb nicht. Gezaehlt werden muss `Faehigkeit|Mangel`.
    """
    router = Router()
    orch = _orch(plans=[[_SCOUT, _browser_open(ziel="a")],
                        [_browser_open(frage="b")],
                        [_browser_open(thema="c")]],
                 router=router)
    final = _drive(orch, objective="Finde den Preis und schreib ihn auf.")

    require_equal(final.state, S.FAILED, final.state)
    require_equal(final.failure_category, "planner_invalid_step",
                  f"der Lauf endete als {final.failure_category} — das verschweigt, "
                  "wer nicht weiterkam")
    versuche = [s for s in orch.ledger.steps_for_run(final.run_id)
                if s.capability == "browser_open"]
    require_equal(len(versuche), 2,
                  f"der ungueltige Schritt lief {len(versuche)}-mal statt zweimal")
    require(not [c for c in router.calls if c["name"] == "browser_open"],
            "der Router hat den ungueltigen Schritt trotzdem gesehen")


def t_the_planner_learns_the_reason_not_just_the_failure():
    """Vorher stand in seinem Kontext nur „Eine Faehigkeit scheiterte". Daraus
    konnte er nichts lernen — und schlug denselben Schritt wieder vor."""
    orch = _orch(plans=[[_SCOUT, _browser_open(ziel="a")], [_SCOUT]])
    _drive(orch, objective="Finde den Preis und schreib ihn auf.", ticks=10)
    planner = orch.planner
    require(len(planner.contexts) >= 2,
            f"es gab keine Nachplanung: {len(planner.contexts)} Ereignisse")
    nachplan = planner.contexts[1]
    require("browser_open" in nachplan and "url" in nachplan,
            f"der Grund fehlt im Planerkontext der Nachplanung: {nachplan!r}")


# =====================================================================
# D — Befunde ueberleben
# =====================================================================

def t_findings_survive_a_later_failure_and_the_notice_stays_honest():
    """Ein spaeterer Fehlschlag loescht kein Ergebnis — und die Meldung sagt
    beides: was da ist, und dass der Auftrag nicht fertig ist."""
    proactive = Proactive()
    orch = _orch(plans=[[_SCOUT, _browser_open(ziel="a")],
                        [_browser_open(frage="b")]],
                 proactive=proactive)
    final = _drive(orch, objective="Finde den Preis und schreib ihn auf.")
    require_equal(final.state, S.FAILED, final.state)

    meldungen = [i for i in proactive.items if i.get("run_id") == final.run_id]
    require(meldungen, "es kam ueberhaupt keine Meldung")
    letzte = meldungen[-1]
    require(letzte["findings"], "die Meldung kam wieder mit findings=[]")
    require(any("40" in f for f in letzte["findings"]),
            f"das Ergebnis fehlt in der Meldung: {letzte['findings']}")
    require(any(f.startswith("Quelle:") for f in letzte["findings"]),
            f"die Quellen fehlen in der Meldung: {letzte['findings']}")
    require("nicht" in letzte["summary"].lower(),
            f"die Meldung behauptet Erfolg: {letzte['summary']}")


def t_every_notice_carries_the_run_id():
    """Ohne `run_id` ist eine Meldung nicht auf ihren Lauf zurueckzufuehren."""
    proactive = Proactive()
    orch = _orch(plans=[[_SCOUT]], proactive=proactive)
    final = _drive(orch)
    require(proactive.items, "keine Meldung abgelegt")
    for item in proactive.items:
        require_equal(item.get("run_id"), final.run_id,
                      f"Meldung ohne passende Laufkennung: {item.get('run_id')}")
        require_equal(item.get("content_trust"), "untrusted_executor",
                      "die Kennzeichnung als fremde Arbeit fehlt")


def t_the_artifact_carries_the_findings_and_their_sources():
    """Das Ergebnis liegt als Artefakt — strukturiert, nicht als Erzaehlung."""
    orch = _orch(plans=[[_SCOUT]])
    final = _drive(orch)
    artefakte = orch.ledger.artifacts_for_run(final.run_id)
    berichte = [a for a in artefakte if a.kind == "report"]
    require(berichte, f"kein Bericht abgelegt: {artefakte}")
    body = json.loads(open(berichte[0].path, encoding="utf-8").read())
    require(body["befunde"], "der Bericht hat keine Befunde")
    require_equal(len(body["quellen"]), 3,
                  f"die Quellen fehlen im Bericht: {body['quellen']}")
    require_equal(body["herkunft"], "untrusted_executor",
                  "der Bericht ist nicht als fremde Arbeit gekennzeichnet")


def t_the_ledger_keeps_structured_findings_and_no_transcript():
    """Befunde ja, Rohtext nein. Der Rohauszug ist fuer einen Menschen, der
    nachsieht — nicht fuer Buch, Meldung oder Artefakt."""
    proactive = Proactive()

    class Geschwaetzig(Researcher):
        async def research(self, arguments):
            self.calls.append(arguments)
            return {"zusammenfassung": LIVE_ANSWER, "quellen": LIVE_SOURCES,
                    "offene_fragen": []}

    orch = _orch(plans=[[_SCOUT]], researcher=Geschwaetzig(), proactive=proactive)
    final = _drive(orch)
    artefakt = [a for a in orch.ledger.artifacts_for_run(final.run_id)
                if a.kind == "report"][0]
    body = open(artefakt.path, encoding="utf-8").read()
    require_equal(sorted(json.loads(body)),
                  ["befunde", "herkunft", "lauf", "quellen"],
                  "das Artefakt traegt Felder, die niemand benannt hat")
    for step in orch.ledger.steps_for_run(final.run_id):
        require(len(step.summary or "") <= 600,
                f"eine Schrittzusammenfassung sprengt den Deckel: {len(step.summary)}")

    # Und der eigentliche Punkt: der ROHAUSZUG bleibt draussen. Beim Hermes-Weg
    # ist er mit der Zusammenfassung identisch, deshalb ist er hier ausdruecklich
    # verschieden — sonst prueft der Test nichts.
    marke = "ROHTEXT-GEDANKENGANG-DARF-NIRGENDWO-STEHEN"
    geschwaetzig = SpecialistResult(
        role="scout", provider="hermes", question="q", ok=True,
        findings=[LIVE_ANSWER], evidence=LIVE_SOURCES,
        recommended_path=LIVE_ANSWER, raw_excerpt=marke)
    context = O.RunContext(run_id="ar-x", task_id="at-x", scope="research",
                           ledger=orch._contexts.get(final.run_id).ledger
                           if orch._contexts.get(final.run_id) else _budget())
    orch._keep_findings(context, geschwaetzig)
    require(marke not in " ".join(context.findings + context.sources),
            f"der Rohauszug ist in die Befunde gewandert: {context.findings}")
    require(any(LIVE_ANSWER[:30] in f for f in context.findings),
            f"der eigentliche Befund fehlt: {context.findings}")
    # Und was `_keep_findings` nicht aufnimmt, kann `_write_report` auch nicht
    # ablegen — der Bericht schreibt genau diese zwei Listen.
    require(marke not in json.dumps({"befunde": context.findings,
                                     "quellen": context.sources},
                                    ensure_ascii=False),
            "der Rohauszug haette es in den Bericht geschafft")


# =====================================================================
# Was sich NICHT geaendert haben darf
# =====================================================================

def t_a_genuinely_needed_later_step_still_fails_the_run():
    """Ein wirklich noetiger Schritt, der wirklich scheitert, laesst den Lauf
    weiterhin scheitern. Die Reparatur darf keinen Erfolg erfinden."""
    router = Router(specs={"licht": {"type": "object", "properties": {}}},
                    outcomes=[CapabilityResult(OUT.CAPABILITY_FAILED, "c-9", "licht",
                                               human_message="ging nicht"),
                              CapabilityResult(OUT.CAPABILITY_FAILED, "c-9", "licht",
                                               human_message="ging nicht"),
                              CapabilityResult(OUT.CAPABILITY_FAILED, "c-9", "licht",
                                               human_message="ging nicht")])
    schritt = PL.PlannedStep(kind="capability", capability="licht",
                             instruction="Licht schalten", arguments={})
    orch = _orch(plans=[[schritt]], router=router)
    final = _drive(orch, objective="Schalte bitte das Licht im Wohnzimmer an.")
    require_equal(final.state, S.FAILED, final.state)
    require(router.calls, "der noetige Schritt wurde nicht einmal versucht")


def t_the_approval_behaviour_is_unchanged():
    """Ein freigabepflichtiger Schritt PARKT den Lauf — unveraendert."""
    router = Router(specs={"tuer": {"type": "object",
                                    "properties": {"was": {"type": "string"}},
                                    "required": ["was"]}},
                    outcomes=[CapabilityResult(OUT.APPROVAL_REQUIRED, "c-2", "tuer",
                                               data={"request_id": "ap-1"})])
    schritt = PL.PlannedStep(kind="capability", capability="tuer",
                             arguments={"was": "auf"})
    # Die Freigabe bleibt offen. Ohne diese Zeile laese `read_approval_state`
    # eine unbekannte Kennung als EXPIRED — dann prueft der Test das Verfallen
    # statt des Parkens.
    plane = ControlPlane({"ap-1": {"state": "PENDING",
                                   "expires_at": time.time() + 600}})
    orch = _orch(plans=[[schritt]], router=router, control_plane=plane)
    final = _drive(orch, objective="Mach bitte die Tuer auf.", ticks=8)
    require_equal(final.state, S.WAITING_APPROVAL,
                  f"der Lauf parkte nicht: {final.state}")
    require_equal(len(router.calls), 1, "der Schritt lief mehrfach")


def t_the_hermes_seam_is_untouched_and_never_goes_through_the_router():
    """Der Rechercheweg bleibt der Seam. Kein `deep_*` ueber den Router."""
    router = Router()
    researcher = Researcher()
    orch = _orch(plans=[[_SCOUT]], router=router, researcher=researcher)
    final = _drive(orch)
    require_equal(len(researcher.calls), 1,
                  f"der Seam wurde {len(researcher.calls)}-mal gerufen")
    require(not [c for c in router.calls if c["name"].startswith("deep_")],
            f"eine Deep-Faehigkeit lief ueber den Router: {router.calls}")
    require(final.terminal, f"der Lauf parkte: {final.state}")
    require_equal(final.failure_category, "goal_unverified",
                  final.result_summary or final.failure_category)


def t_a_build_scope_is_not_short_circuited_by_goal_satisfaction():
    """Die vorzeitige Vollendung gilt NUR fuer Recherche. Ein Bau-Lauf muss
    seine Ernte durchlaufen — sonst waere „fertig" wieder eine leere Zusage."""
    verdict = CO.evaluate(scope="build")
    require(not verdict.satisfied, "ein Bau-Lauf wurde vorzeitig beendet")
    require_equal(verdict.reason, "no_work_product", verdict.reason)
    # Und die Gegenrichtung: MIT geerntetem Arbeitsergebnis traegt er.
    require(CO.evaluate(scope="build", work_product="refs/agents/ar-1").satisfied,
            "das geerntete Arbeitsergebnis traegt die Erfuellung nicht mehr")


# =====================================================================
# Die Naht zwischen Deep-Seam und Adapter
# =====================================================================

def t_the_real_seam_envelope_reaches_the_adapter_intact():
    """Der ECHTE Seam erzeugt den Umschlag, der ECHTE Adapter liest ihn.

    Genau diese Naht war der Grund, warum zwei Live-Laeufe ohne Ergebnis
    endeten: `_settled` legt die Schemafelder unter `ergebnis`, der Adapter las
    sie eine Ebene zu hoch. Sichtbar war das nicht, weil `quellen`
    ZUSAETZLICH oben steht — `ok = bool(summary or sources)` war also wahr, der
    Schritt galt als gelungen, und verloren war nur die Antwort.

    Nachgebaut wird hier nichts: der Umschlag kommt aus `DeepCapabilities`
    selbst. Aendert sich seine Form, bricht dieser Test — nicht ein Live-Lauf.
    """
    from solvio.capabilities.deep import DeepCapabilities
    from solvio.contracts.deep_runtime import (DeepTaskResult, DeepTaskStatus,
                                               Source, TrustLevel)
    from solvio.agent_runtime import specialists as SP

    class Runtime:
        async def get_status(self, task_id):
            return DeepTaskStatus.SUCCEEDED

        async def get_result(self, task_id):
            return DeepTaskResult(
                success=True,
                data={"zusammenfassung": LIVE_ANSWER, "quellen": LIVE_SOURCES,
                      "offene_fragen": []},
                sources=[Source(ref=u, trust_level=TrustLevel.UNTRUSTED_WEB)
                         for u in LIVE_SOURCES])

    umschlag = _run(DeepCapabilities(Runtime()).status({"task_id": "dt-1"}))
    # Genau die zwei Zusagen, auf die der Adapter angewiesen ist — und nur die.
    # `abgeschlossen`/`lage` gibt es erst mit Deep Research Reliability V1; wer
    # hier darauf pruefte, koppelte diesen Milestone an einen anderen.
    require(isinstance(umschlag.get("ergebnis"), dict),
            f"die Schemafelder liegen nicht unter `ergebnis`: {sorted(umschlag)}")
    require(isinstance(umschlag.get("quellen"), list) and umschlag["quellen"],
            f"die Quellen liegen nicht oben im Umschlag: {sorted(umschlag)}")
    require("zusammenfassung" not in umschlag,
            "die Zusammenfassung liegt oben — dann waere der Adapter nie kaputt "
            "gewesen und dieser Test pruefte nichts")

    class Seam:
        async def research(self, arguments):
            return umschlag

        async def status(self, arguments):
            return umschlag

    lauf = _run(SP.run_specialist(
        SP.SpecialistRequest(profile="researcher/hermes", objective=LIVE_GOAL,
                             workdir="", run_id="ar-naht"),
        researcher=Seam()))
    ergebnis = lauf.result
    require(ergebnis.ok, f"der Schritt gilt als gescheitert: {ergebnis.reason}")
    require(ergebnis.usable,
            "der Schritt gilt als gelungen, hat aber keine verwertbare Antwort — "
            "genau der Live-Befund")
    require(LIVE_ANSWER[:40] in (ergebnis.recommended_path or ""),
            f"die Antwort kam nicht an: {ergebnis.recommended_path!r}")
    require_equal(len(ergebnis.evidence), 3,
                  f"die Quellen kamen nicht an: {ergebnis.evidence}")
    # Frueher stand hier: „und daraus muss die Zielerfuellung folgen". Genau
    # diese Folgerung ist seit FIX 2 abgeschafft — eine vollstaendige Antwort
    # ist ein verfuegbares ERGEBNIS, kein Erfuellungsnachweis. Der Gegenstand
    # dieser Pruefung ist die NAHT, und die ist oben vollstaendig belegt.
    require(not CO.evaluate(scope="research").satisfied,
            "eine vollstaendige Antwort gilt wieder als Zielerfuellung")


def t_a_flat_answer_is_still_understood():
    """Ein Seam, der eines Tages direkt das Schema liefert, bleibt lesbar."""
    from solvio.agent_runtime import specialists as SP

    class Flach:
        async def research(self, arguments):
            return {"zusammenfassung": LIVE_ANSWER, "quellen": LIVE_SOURCES,
                    "offene_fragen": []}

        async def status(self, arguments):
            return {}

    lauf = _run(SP.run_specialist(
        SP.SpecialistRequest(profile="researcher/hermes", objective=LIVE_GOAL,
                             workdir="", run_id="ar-flach"),
        researcher=Flach()))
    require(lauf.result.usable, "die flache Form wird nicht mehr verstanden")
    require_equal(len(lauf.result.evidence), 3, str(lauf.result.evidence))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
