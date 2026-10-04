"""Autopilot A4 — der Zyklus, der den Eigentuemer als Nachrichtenbus ersetzt.

Die Suite stellt den Kreis: BUILD → TEST → REVIEW → FIX → TEST → REVIEW, mit
einem echten git-Arbeitsbereich, einem echten Test-Gate und einem gestellten
Technical Lead. Gestellt ist NUR der Lead — Bauen, Messen und Buchen laufen
echt, sonst prueft die Suite ihre eigene Erwartung.

Die schaerfste Zusicherung ist dieselbe wie in A1, jetzt aber im ganzen Kreis:
**ein Lead, der READY sagt, waehrend das Gate rot ist, bekommt kein READY.**
Der Treiber verwandelt es in eine Reparaturrunde und schreibt den Grund ins
Buch — er streitet nicht mit dem Modell, er misst.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a4-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ.setdefault("SOLVIO_AUTOPILOT_LOCK", _SANDBOX + "/ap.lock")

from solvio.autopilot import builders as B     # noqa: E402
from solvio.autopilot import context as CTX    # noqa: E402
from solvio.autopilot import contract as C     # noqa: E402
from solvio.autopilot import driver as D       # noqa: E402
from solvio.autopilot import lead as LEAD      # noqa: E402
from solvio.autopilot import store as S        # noqa: E402

BASIS = {
    "milestone_id": "probe-a4", "version": "1.0.0",
    "objective": "Lass das Gate gruen werden.",
    "acceptance_criteria": [
        {"key": "gate", "text": "Gate gruen", "evidence_type": "DETERMINISTIC"},
        {"key": "lesbar", "text": "Der Code ist lesbar",
         "evidence_type": "REVIEW_SUPPORTED"},
    ],
}

GATE_GRUEN = ("Suites: 1   EXPECTED=2 EXECUTED=2 PASSED=2 FAILED=0 SKIPPED=0\n"
              "Missing=0 BaselineDrift=0")
GATE_ROT = ("Suites: 1   EXPECTED=2 EXECUTED=2 PASSED=1 FAILED=1 SKIPPED=0\n"
            "Missing=0 BaselineDrift=0")


# --------------------------------------------------------------------- Werkzeug
def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
           "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"}
    return subprocess.run(["git", "-C", repo, *args], env=env,
                          capture_output=True, text=True)


def _arbeitsbereich(*, gate: str = GATE_ROT) -> str:
    """Ein echter Klon mit einem echten (kleinen) Test-Gate.

    Das Gate ist ein Skript, das eine Datei liest — so kann ein Builder es
    wirklich gruen machen, statt dass die Suite so tut als ob.
    """
    repo = tempfile.mkdtemp(prefix="werk-", dir=_SANDBOX)
    os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
    with open(os.path.join(repo, "scripts", "run_tests.py"), "w") as fh:
        fh.write(
            "import os\n"
            "gruen = os.path.isfile(os.path.join(os.path.dirname(__file__),"
            " '..', 'REPARIERT'))\n"
            f"print({gate!r} if not gruen else {GATE_GRUEN!r})\n"
            "raise SystemExit(0 if gruen else 1)\n")
    venv = os.path.join(repo, ".venv", "bin")
    os.makedirs(venv, exist_ok=True)
    os.symlink(sys.executable, os.path.join(venv, "python"))
    with open(os.path.join(repo, "uv.lock"), "w") as fh:
        fh.write("version = 1\n")
    _git(repo, "init", "--quiet", "-b", "haupt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "start")
    return repo


class _Lead:
    """Ein gestellter Technical Lead: eine Liste von Urteilen, der Reihe nach.

    Er ersetzt NUR den Modellaufruf. Die Pruefung `validate()` laeuft echt —
    ein Urteil, das der Core nicht annehmen wuerde, wird hier auch nicht
    angenommen.
    """

    def __init__(self, urteile: list[dict]) -> None:
        self.urteile = list(urteile)
        self.gesehen: list[str] = []
        self.calls = 0

    async def judge(self, *, context, allowed_builders, open_finding_ids,
                    criterion_keys, evidence_ids, deterministic_keys,
                    tier="small"):
        self.calls += 1
        self.gesehen.append(context)
        roh = self.urteile.pop(0) if self.urteile else {
            "verdict": "NEEDS_HUMAN", "next_action": "human_required",
            "rationale": "keine Urteile mehr gestellt"}
        urteil = LEAD.validate(json.dumps(roh),
                               allowed_builders=set(allowed_builders),
                               open_finding_ids=set(open_finding_ids),
                               criterion_keys=set(criterion_keys),
                               evidence_ids=set(evidence_ids),
                               deterministic_keys=set(deterministic_keys))
        urteil.model = "gpt-5.4-mini"
        urteil.tokens = 1_000
        return urteil


def _ledger() -> S.AutopilotLedger:
    return S.AutopilotLedger(tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3")


def _welt(*, adapters=None, urteile=None, gate=GATE_ROT):
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    led.create_milestone(C.parse(BASIS))
    werk = _arbeitsbereich(gate=gate)
    lead = _Lead(urteile or [])
    fahrer = D.Driver(led, workspace=werk,
                      adapters=adapters or {"synthetic": B.SyntheticBuilder()},
                      lead=lead, python=sys.executable)
    return led, fahrer, lead, werk


# ------------------------------------------------------- der Kreis dreht sich
def t_three_full_rounds_run_without_a_human() -> None:
    """Das Erfolgskriterium des Milestones, im Kleinen.

    Ein Auftrag, drei vollstaendige Runden Builder→Test→Lead — und der
    Eigentuemer kommt darin nicht vor.
    """
    reparatur = B.SyntheticBuilder(writes={"schritt.txt": "eins\n"})
    led, fahrer, lead, werk = _welt(
        adapters={"synthetic": reparatur},
        urteile=[
            {"verdict": "FIX", "next_action": "fix", "rationale": "Gate rot",
             "task": "repariere"},
            {"verdict": "FIX", "next_action": "fix", "rationale": "immer noch rot",
             "task": "repariere weiter"},
            {"verdict": "FIX", "next_action": "fix", "rationale": "beinahe",
             "task": "letzter Versuch"},
        ])
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=3))
    require_equal(lead.calls, 3, f"es liefen {lead.calls} Review-Runden statt 3")
    require(zustand not in S.TERMINAL_STATES,
            "der Lauf endete vorzeitig terminal")
    arten = [e["kind"] for e in led.events("probe-a4")]
    require(arten.count("state_changed") >= 6,
            f"zu wenige Zustandswechsel: {arten.count('state_changed')}")
    require(reparatur.calls >= 3, f"der Builder lief nur {reparatur.calls}x")
    require(led.milestone("probe-a4").state not in S.PARKED_STATES,
            "der Lauf wurde geparkt, obwohl nichts zu entscheiden war")


def t_a_red_gate_becomes_a_fix_round_not_a_question() -> None:
    """Ein roter Test ruft niemanden. Er erzeugt eine Reparaturrunde."""
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "FIX", "next_action": "fix",
                  "rationale": "ein Test ist rot", "task": "repariere modul_x"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    zustand = led.milestone("probe-a4")
    require_equal(zustand.state, S.FIXING, f"falscher Zustand: {zustand.state}")
    require("repariere modul_x" in zustand.current_task,
            f"die Aufgabe kam nicht an: {zustand.current_task}")
    require(not led.open_boundaries("probe-a4"),
            "ein roter Test hat eine Nutzergrenze geoeffnet")


def t_the_lead_cannot_talk_a_red_gate_into_ready() -> None:
    """Dieselbe Zusicherung wie in A1 — jetzt im echten Kreis.

    Der Lead sagt READY, das Gate ist rot. Der Treiber streitet nicht: er
    misst, verweigert, schreibt den Grund ins Buch und macht daraus eine
    Reparaturrunde.
    """
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "READY", "next_action": "ready",
                  "rationale": "sieht gut aus", "task": ""}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    zustand = led.milestone("probe-a4")
    require(zustand.state != S.READY,
            "ein rotes Gate wurde durch ein Urteil READY")
    require_equal(zustand.state, S.FIXING,
                  f"unerwarteter Zustand nach dem verweigerten READY: {zustand.state}")
    texte = [e["summary"] for e in led.events("probe-a4")]
    require(any("READY verweigert" in t and "gate_not_green" in t for t in texte),
            f"der Grund steht nicht im Buch: {texte[:6]}")


def t_a_green_gate_and_a_ready_verdict_reach_ready() -> None:
    """Die Gegenprobe. Eine Maschine, die nie READY sagt, ist auch kaputt."""
    fertig = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    led, fahrer, lead, _ = _welt(
        adapters={"synthetic": fertig},
        urteile=[{"verdict": "READY", "next_action": "ready",
                  "rationale": "alles belegt",
                  "proven": []}])
    # Das REVIEW_SUPPORTED-Kriterium braucht eine Evidence-Referenz; die kennt
    # der Lead erst im Lauf. Deshalb zwei Runden: erst messen, dann urteilen.
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    zustand = led.milestone("probe-a4")
    bewiesen, gesamt = led.acceptance_counts("probe-a4")
    require_equal(bewiesen, 1,
                  f"das Gate hat das DETERMINISTIC-Kriterium nicht bewiesen "
                  f"({bewiesen}/{gesamt})")
    require(zustand.state != S.READY,
            "READY trotz unvollstaendiger Akzeptanz")

    # Jetzt das zweite Kriterium mit Beleg — und READY traegt.
    beleg = [e for e in led.events("probe-a4") if e["kind"] == "evidence_recorded"]
    ev = [r for r in led.events("probe-a4") if r["kind"] == "evidence_recorded"][0]["ref"]
    lead.urteile = [{"verdict": "READY", "next_action": "ready",
                     "rationale": "jetzt vollstaendig",
                     "proven": [{"key": "lesbar", "evidence_ref": ev}]}]
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(led.milestone("probe-a4").state, S.READY,
                  f"READY blieb aus: {led.acceptance_counts('probe-a4')}")


def t_the_gate_only_proves_criteria_that_mean_the_gate() -> None:
    """Ein gruenes Gate beweist nicht jede Behauptung, die jemand
    DETERMINISTIC genannt hat."""
    led, fahrer, lead, _ = _welt(
        adapters={"synthetic": B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})},
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "x"}])
    led._db.execute(
        "INSERT INTO criteria (milestone_id, key, evidence_type, text, state)"
        " VALUES (?,?,?,?,?)",
        ("probe-a4", "netzwerk_aus", "DETERMINISTIC", "kein Netz", "open"))
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    zustaende = {k["key"]: k["state"] for k in led.criteria("probe-a4")}
    require_equal(zustaende["gate"], S.CRIT_PROVEN, "das Gate-Kriterium fehlt")
    require_equal(zustaende["netzwerk_aus"], S.CRIT_OPEN,
                  "ein fremdes DETERMINISTIC-Kriterium wurde vom Gate bewiesen")


def t_a_needs_human_verdict_parks_with_a_question() -> None:
    """Eine echte Produktentscheidung parkt — mit einer Frage, nicht stumm."""
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "NEEDS_HUMAN", "next_action": "human_required",
                  "rationale": "Zwei Formen moeglich, das ist deine Wahl."}])
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=2))
    require_equal(zustand, S.HUMAN_REQUIRED, f"nicht geparkt: {zustand}")
    grenzen = led.open_boundaries("probe-a4")
    require_equal(len(grenzen), 1, f"falsche Zahl offener Grenzen: {len(grenzen)}")
    require("deine Wahl" in grenzen[0]["question"], "die Frage fehlt")
    require_equal(led.milestone("probe-a4").state_before_park, S.REVIEWING,
                  "der Vorzustand wurde nicht gemerkt")


def t_the_context_the_lead_sees_carries_the_contract_hash() -> None:
    """Der Lead kann keine andere Fassung meinen, ohne dass es auffaellt."""
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "x"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require(lead.gesehen, "der Lead hat nie einen Context gesehen")
    text = lead.gesehen[0]
    require(led.milestone("probe-a4").contract_hash in text,
            "der Contract-Hash fehlt im Context")
    require("READ-ONLY" in text, "die Projektion ist nicht als read-only markiert")
    require("CONTRACT_CHANGE_REQUIRED" in text,
            "der Context sagt nicht, wie eine Aenderung geht")


def t_the_ledger_records_what_each_round_cost() -> None:
    """Ohne Verbrauchsbuch waere jede Effizienzaussage eine Behauptung."""
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "x"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    verbrauch = led.usage_summary("probe-a4")
    require(S.ROLE_BUILDER in verbrauch, "der Builder-Verbrauch fehlt")
    require(S.ROLE_LEAD in verbrauch, "der Lead-Verbrauch fehlt")
    require_equal(verbrauch[S.ROLE_LEAD]["provider_tokens"], 1_000,
                  "die gemeldeten Lead-Token fehlen")
    require_equal(verbrauch[S.ROLE_BUILDER]["eintraege_ohne_tokenangabe"], 1,
                  "der Builder ohne Tokenangabe wurde nicht als solcher gefuehrt")


def t_a_credential_in_the_workspace_stops_everything() -> None:
    """Amendment 6 im Kreis: fail closed, und der Mensch wird gerufen.

    Das ist der EINE Fall, in dem ein Baufehler zum Menschen fuehrt — nicht
    weil er ein Bug ist, sondern weil er ein Sicherheitsbefund ist.
    """
    schmuggler = B.SyntheticBuilder(
        writes={"auth.json": '{"tokens": {"access_token": "geheim"}}\n'})
    led, fahrer, lead, werk = _welt(adapters={"synthetic": schmuggler})
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=2))
    require_equal(zustand, S.HUMAN_REQUIRED,
                  f"ein Kredentialfund hielt nicht an: {zustand}")
    befunde = led.findings("probe-a4")
    require(any(f.origin == "security" and f.severity == "blocker"
                for f in befunde),
            f"kein Security-Finding: {[(f.origin, f.severity) for f in befunde]}")
    arten = [e["kind"] for e in led.events("probe-a4")]
    require("security_finding" in arten, "der Befund steht nicht in der Chronik")
    stand = subprocess.run(["git", "-C", werk, "log", "--oneline"],
                           capture_output=True, text=True).stdout
    require("autopilot-checkpoint" not in stand,
            "es wurde trotz Kredentialfund committet")


def t_reconcile_closes_an_interrupted_phase_honestly() -> None:
    """Nach `kill -9` wird nichts still als Erfolg verbucht."""
    led, fahrer, lead, _ = _welt()
    phase = led.start_phase("probe-a4", kind="build", builder="synthetic")
    require_equal(len(led.open_phases("probe-a4")), 1, "die Phase ist nicht offen")

    geschlossen = fahrer.reconcile("probe-a4")
    require_equal(geschlossen, [phase], "die offene Phase wurde nicht gefunden")
    require_equal(len(led.open_phases("probe-a4")), 0, "sie blieb offen")
    zeile = [p for p in led.phases("probe-a4") if p["phase_id"] == phase][0]
    require_equal(zeile["state"], "interrupted",
                  f"sie wurde als {zeile['state']} verbucht")
    arten = [e["kind"] for e in led.events("probe-a4")]
    require("interrupted" in arten and "recovered" in arten,
            f"die Chronik verschweigt den Abbruch: {arten}")


def t_the_lead_is_told_which_evidence_it_may_cite() -> None:
    """Eine Regel, die eine Referenz verlangt und keine nennt, ist eine Falle.

    Live gelernt in der A7-Abnahme: ein `REVIEW_SUPPORTED`-Kriterium darf nur
    mit einer Evidence-Referenz auf `proven` gesetzt werden — aber der Context
    nannte keine einzige. Der Technical Lead musste raten, `validate()`
    verwarf sein Urteil mit `proven_without_known_evidence`, und der Milestone
    kam nie voran. Die Pruefung hatte recht; der Context war unvollstaendig.
    """
    led, fahrer, lead, _ = _welt(
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "x"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    require(lead.gesehen, "der Lead sah keinen Context")
    text = lead.gesehen[0]
    require("Verfuegbare Belege" in text,
            f"der Context nennt keine zitierbaren Belege:\n{text[:400]}")

    # Und die genannte Kennung muss eine ECHTE sein, keine erfundene.
    belege = [e["ref"] for e in led.events("probe-a4")
              if e["kind"] == "evidence_recorded"]
    require(belege, "es wurde keine Evidence gebucht")
    require(any(b in text for b in belege),
            "keine der gebuchten Kennungen steht im Context")


def t_a_verdict_citing_the_named_evidence_is_accepted() -> None:
    """Die Gegenprobe: mit einer genannten Kennung traegt das Urteil.

    Ohne diese Haelfte koennte der Context Kennungen nennen, die `validate()`
    trotzdem verwirft — und niemand haette es gemerkt.
    """
    fertig = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    led, fahrer, lead, _ = _welt(
        adapters={"synthetic": fertig},
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "erst messen"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    # Die Kennung, die der Lead im Context gesehen hat, jetzt zitieren.
    import re
    treffer = re.findall(r"\bev-[0-9a-f]{12}\b", lead.gesehen[0])
    require(treffer, f"im Context stand keine Belegkennung: {lead.gesehen[0][:300]}")
    lead.urteile = [{"verdict": "READY", "next_action": "ready",
                     "rationale": "belegt",
                     "proven": [{"key": "lesbar", "evidence_ref": treffer[0]}]}]
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    zustaende = {k["key"]: k["state"] for k in led.criteria("probe-a4")}
    require_equal(zustaende["lesbar"], S.CRIT_PROVEN,
                  f"das zitierte Urteil trug nicht: {zustaende}")
    require(not [f for f in led.findings("probe-a4") if f.origin == "lead"],
            "ein gueltiges Urteil erzeugte trotzdem ein Finding")


def t_the_context_names_the_criteria_the_lead_may_not_judge() -> None:
    """Eine Regel, die nur in der Pruefung steht, ist eine Falle — zweimal.

    Live gelernt in der A7-Abnahme: `validate()` verwirft jedes Urteil, das ein
    DETERMINISTIC-Kriterium `proven` setzt. Der Context nannte diese Regel nie,
    der Lead setzte `gate`, und das ganze Urteil fiel mit
    `deterministic_criterion_judged` — bei gruenem Gate.
    """
    led = _ledger()
    led.create_milestone(C.parse({
        "milestone_id": "probe-det", "version": "1.0.0", "objective": "Z",
        "acceptance_criteria": [
            {"key": "gate", "text": "Gruen.", "evidence_type": "DETERMINISTIC"},
            {"key": "lesbar", "text": "Lesbar.",
             "evidence_type": "REVIEW_SUPPORTED"}]}))
    paket = CTX.compile_context(led, "probe-det", gate_summary="3056/3056")

    require("Gueltige Schluessel, vollstaendig:" in paket.text,
            f"der Context zaehlt die gueltigen Schluessel nicht auf:"
            f"\n{paket.text[:600]}")
    # Und die Zeile muss den Schluessel als Schluessel ausweisen — nicht als
    # Anfang eines Satzes. Live gemessen: der Lead nannte einmal
    # `naht (REVIEW_SUPPORTED)`, weil er die ganze Zeile bis zum Doppelpunkt
    # genommen hatte. Das war eine faire Lesart der alten Form.
    require("key=gate" in paket.text and "key=lesbar" in paket.text,
            f"die Kriterienzeilen weisen den Schluessel nicht aus:"
            f"\n{paket.text[:600]}")
    require("AUSSCHLIESSLICH die Messung" in paket.text,
            f"die Regel steht nicht im Context:\n{paket.text[:600]}")
    kopf = paket.text.split("Diese Kriterien setzt")[1][:200]
    require("gate" in kopf, f"das gemeinte Kriterium wird nicht genannt: {kopf}")
    require("lesbar" not in kopf,
            f"ein REVIEW_SUPPORTED-Kriterium wurde mitgesperrt: {kopf}")


def t_the_measurement_stands_above_the_older_claims_about_it() -> None:
    """Ein Finding ist von frueher, die Messung ist von jetzt.

    Live gemessen: bei 3056/3056 und Drift 0 urteilte der Lead „das Test-Gate
    ist noch rot" — weil ueber der Messung ein offenes Finding aus der
    Vorrunde stand, das genau das behauptete.
    """
    led = _ledger()
    led.create_milestone(C.parse({
        "milestone_id": "probe-ord", "version": "1.0.0", "objective": "Z",
        "acceptance_criteria": [
            {"key": "a", "text": "t", "evidence_type": "REVIEW_SUPPORTED"}]}))
    led.open_finding("probe-ord", severity="blocker", title="Test-Gate rot",
                     detail="aus der Vorrunde", origin="lead")
    paket = CTX.compile_context(led, "probe-ord", gate_summary="3056/3056 gruen")

    require(paket.text.index("## Test-Gate") < paket.text.index("## Offene Findings"),
            "die alten Behauptungen stehen ueber der aktuellen Messung")
    require("aus frueheren Runden" in paket.text,
            "die Findings sind nicht als Vergangenheit gekennzeichnet")
    require("close_findings" in paket.text,
            "dem Lead wird nicht gesagt, wie er ein erledigtes Finding schliesst")


# ------------------------------------------- der Contract bleibt Core-Sache
def _vorschlag(werk: str, inhalt: str) -> None:
    ziel = os.path.join(werk, D.PROPOSAL_FILE)
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    with open(ziel, "w", encoding="utf-8") as fh:
        fh.write(inhalt)


def t_a_contract_proposal_in_the_worktree_stops_the_run() -> None:
    """Die Zusage aus dem Context war bis zur A7-Abnahme leer.

    Jeder Builder liest „READ-ONLY. Eine Aenderung im Arbeitsbaum ist ein
    Vorschlag und erzeugt CONTRACT_CHANGE_REQUIRED." — und nichts sah nach.
    Ein angekuendigtes Tor, durch das jeder unbemerkt geht, ist schlimmer als
    keines: es steht in der Dokumentation als Sicherheit.
    """
    led, fahrer, lead, werk = _welt()
    gefaelscht = dict(BASIS)
    # Dieselbe Fassungsnummer, anderes Ziel — der gefaehrlichste Fall, weil er
    # in jeder Uebersicht unveraendert aussieht.
    gefaelscht["objective"] = "Und ausserdem: raeum das Repository auf."
    _vorschlag(werk, json.dumps(gefaelscht))

    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=2))
    require_equal(zustand, S.HUMAN_REQUIRED,
                  f"der Vorschlag hielt den Lauf nicht an: {zustand}")
    grenzen = led.open_boundaries("probe-a4")
    require_equal(len(grenzen), 1, f"keine Grenze geoeffnet: {grenzen}")
    require_equal(grenzen[0]["kind"], "contract_change",
                  f"falsche Grenzart: {grenzen[0]['kind']}")
    require("same_version_different_content" in grenzen[0]["question"],
            f"der Grund fehlt in der Frage: {grenzen[0]['question']}")
    require_equal(len(lead.gesehen), 0,
                  "es wurde trotz offener Contract-Frage weitergearbeitet")

    # Und der kanonische Contract im Ledger ist unveraendert.
    require_equal(json.loads(led.milestone("probe-a4").contract_json)["objective"],
                  BASIS["objective"],
                  "der Vorschlag hat den kanonischen Contract veraendert")


def t_an_unreadable_proposal_is_not_treated_as_no_change() -> None:
    """Wer den Vorschlag nicht lesen kann, weiss nicht, dass er harmlos ist."""
    led, fahrer, lead, werk = _welt()
    _vorschlag(werk, "{ das ist kein JSON")

    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=2))
    require_equal(zustand, S.HUMAN_REQUIRED,
                  f"ein unlesbarer Vorschlag lief einfach durch: {zustand}")
    grenzen = led.open_boundaries("probe-a4")
    require("unreadable_proposal" in grenzen[0]["question"],
            f"der Grund benennt die Unlesbarkeit nicht: {grenzen[0]['question']}")


def t_an_identical_proposal_is_not_a_boundary() -> None:
    """Ein Tor, das bei jedem Vorbeigehen zuschlaegt, wird abgeschaltet."""
    led, fahrer, lead, werk = _welt(
        urteile=[{"verdict": "FIX", "next_action": "fix", "rationale": "x"}])
    _vorschlag(werk, json.dumps(BASIS))

    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(len(led.open_boundaries("probe-a4")), 0,
                  "ein unveraenderter Vorschlag oeffnete eine Grenze")
    require(len(lead.gesehen) >= 1, "der Lauf kam nicht bis zum Review")


# ------------------------------------------- C3: die READY-Semantik
def _fertige_lage(urteil: dict):
    """Eine Lage, die messbar fertig ist — Gate gruen, jedes Kriterium bewiesen.

    Der Builder schreibt `REPARIERT`, also gibt das Gate wirklich 0 zurueck;
    ein gruen aussehender Text mit rotem Rueckgabewert ist hier ausdruecklich
    nicht gut genug. Danach fehlt nur noch das REVIEW_SUPPORTED-Kriterium, das
    der Lead im ersten Lauf noch nicht belegen konnte.
    """
    fertig = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    led, fahrer, lead, _ = _welt(
        adapters={"synthetic": fertig},
        urteile=[{"verdict": "FIX", "next_action": "fix",
                  "rationale": "erst messen"}])
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    beleg = [e["ref"] for e in led.events("probe-a4")
             if e["kind"] == "evidence_recorded"][0]
    for k in led.criteria("probe-a4"):
        if k["state"] != S.CRIT_PROVEN:
            led.set_criterion("probe-a4", k["key"], state=S.CRIT_PROVEN,
                              evidence_ref=beleg)
    lead.urteile = [urteil]
    return led, fahrer, beleg


def _hat_es_versucht(led) -> bool:
    """Hat der Fahrer READY ueberhaupt angefasst?

    Diese Frage ist scharf, wo der Endzustand stumpf ist: die Vorbedingung in
    `machine.transition` faengt jeden unberechtigten Versuch ohnehin ab, der
    Milestone landet also so oder so in FIXING. Wer nur den Endzustand prueft,
    merkt deshalb nicht, dass der Fahrer bei UNFERTIGER Lage nach READY
    gegriffen hat — und hinterlaesst ein „READY verweigert" im Buch, das nach
    einem gescheiterten Abschluss aussieht, wo gar keiner beantragt war.
    """
    return any("ohne neuen Befund" in (e["summary"] or "")
               or "READY verweigert" in (e["summary"] or "")
               for e in led.events("probe-a4", limit=999))


def t_a_bare_fix_cannot_hold_a_finished_milestone() -> None:
    """Live gemessen in der B5-Abnahme, und es war kein Schoenheitsfehler.

    Gate gruen, alle Kriterien `proven`, kein Finding offen — und der Lead
    sagte trotzdem FIX, ohne einen einzigen neuen Befund. Der Milestone waere
    bis zur Rundengrenze gelaufen und haette nichts mehr gefunden.
    """
    led, fahrer, _ = _fertige_lage(
        {"verdict": "FIX", "next_action": "fix", "rationale": "fast"})
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.READY,
                  f"ein nacktes FIX hielt den fertigen Milestone fest: "
                  f"{zustand}")
    gruende = [e["summary"] for e in led.events("probe-a4")
               if e["kind"] == "decision_recorded"]
    require(any("ohne neuen Befund" in g for g in gruende),
            f"der Grund steht nicht im Buch: {gruende[:4]}")


def t_a_bare_build_on_a_finished_milestone_also_ends_it() -> None:
    """BUILD zaehlt genauso — sonst verschiebt sich dieselbe Schleife nur.

    Ein Lead, der bei fertiger Lage „bau weiter" sagt, ohne zu sagen was,
    haelt den Milestone genauso fest wie mit einem nackten FIX.
    """
    _, fahrer, _ = _fertige_lage(
        {"verdict": "BUILD", "next_action": "build", "rationale": "weiter"})
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.READY,
                  f"ein nacktes BUILD hielt den fertigen Milestone fest: "
                  f"{zustand}")


def t_any_finding_from_the_lead_keeps_the_milestone_open() -> None:
    """Der Lead bleibt echter Beurteiler — auch bei einem KLEINEN Befund.

    Absichtlich `minor`, also nicht blockierend: waere die Regel nur „keine
    blockierenden Findings", wuerde dieser Milestone jetzt abgeschlossen und
    der genannte Defekt ginge unter. Nennt der Lead irgendetwas, endet der
    Milestone nicht.
    """
    led, fahrer, _ = _fertige_lage(
        {"verdict": "FIX", "next_action": "fix", "rationale": "noch ein Fund",
         "findings": [{"severity": "minor",
                       "title": "Die Zusicherung prueft den Text",
                       "detail": "nicht das Verhalten"}]})
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.FIXING,
                  f"ein genannter Befund hielt den Milestone nicht: {zustand}")
    offen = [f.title for f in led.findings("probe-a4")]
    require(offen == ["Die Zusicherung prueft den Text"],
            f"der Befund des Leads wurde nicht gebucht: {offen}")
    require(not _hat_es_versucht(led),
            "der Fahrer griff trotz genanntem Befund nach READY")


def t_a_blocking_finding_keeps_the_milestone_open() -> None:
    """Ein offener Blocker aus einer FRUEHEREN Runde zaehlt auch.

    Der Lead nennt diesmal nichts Neues — der Blocker steht schon im Buch.
    Er ist der Grund, und der Fahrer darf READY nicht einmal versuchen.
    """
    led, fahrer, _ = _fertige_lage(
        {"verdict": "FIX", "next_action": "fix", "rationale": "ohne Befund"})
    led.open_finding("probe-a4", severity="blocker", title="Alter Blocker",
                     detail="steht seit Runde eins offen", origin="lead")
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.FIXING,
                  f"ein offener Blocker hielt den Milestone nicht: {zustand}")
    require(not _hat_es_versucht(led),
            "der Fahrer griff trotz offenem Blocker nach READY")


def t_a_green_gate_alone_is_not_enough_for_ready() -> None:
    """Ausdruecklich KEINE Regel „gruenes Gate = READY".

    Hier ist das Gate wirklich gruen und der Lead nennt keinen Befund — aber
    ein Akzeptanzkriterium ist noch offen. Das ist der Fall, der die neue
    Zeile von einer Abkuerzung unterscheidet.
    """
    fertig = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    led, fahrer, _, _ = _welt(
        adapters={"synthetic": fertig},
        urteile=[{"verdict": "FIX", "next_action": "fix",
                  "rationale": "ohne Befund"}])
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    bewiesen, gesamt = led.acceptance_counts("probe-a4")
    require(bewiesen < gesamt,
            f"die Lage war doch vollstaendig: {bewiesen}/{gesamt}")
    beleg = [e["ref"] for e in led.events("probe-a4")
             if e["kind"] == "evidence_recorded"][0]
    require(led.evidence(beleg).ok, "das Gate war gar nicht gruen")
    require_equal(zustand, S.FIXING,
                  f"ein gruenes Gate allein reichte fuer READY: {zustand}")
    require(not _hat_es_versucht(led),
            "der Fahrer griff bei offenem Kriterium nach READY")


def t_a_milestone_without_criteria_is_never_finished() -> None:
    """Kein Kriterium heisst nicht „nichts mehr offen", sondern „nie geprueft".

    Ein Auftrag ohne Akzeptanzkriterien ist der leichteste Weg zu einem
    versehentlichen Abschluss: es ist trivial wahr, dass alle Kriterien
    bewiesen sind, wenn es keine gibt.
    """
    fertig = B.SyntheticBuilder(writes={"REPARIERT": "ja\n"})
    led, fahrer, _, _ = _welt(
        adapters={"synthetic": fertig},
        urteile=[{"verdict": "FIX", "next_action": "fix",
                  "rationale": "ohne Befund"}])
    led._db.execute("DELETE FROM criteria WHERE milestone_id = ?", ("probe-a4",))
    led._db.commit()
    require_equal(led.acceptance_counts("probe-a4")[1], 0,
                  "die Kriterien wurden nicht entfernt")
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.FIXING,
                  f"ein Milestone ohne Kriterien wurde fertig: {zustand}")
    require(not _hat_es_versucht(led),
            "der Fahrer griff ohne ein einziges Kriterium nach READY")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
