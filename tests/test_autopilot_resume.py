"""Autopilot A6 — Nutzergrenzen, Resume und Status.

Die Resume-Zusicherung ist die teuerste: ein Treiber wird mit `kill -9`
erschlagen — nicht sanft beendet, sondern mitten im Satz —, ein frischer
Prozess uebernimmt, und der Milestone verliert **nichts**: Contract, Commit,
Evidence, Findings, offene Frage. Und nichts wird still als Erfolg verbucht.

Ein `kill -9` ist hier bewusst kein `SIGTERM`: ein Prozess, der aufraeumen
darf, beweist nur, dass sein Aufraeumen funktioniert. Der Ledger muss ohne
Aufraeumen tragen.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a6-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ.setdefault("SOLVIO_AUTOPILOT_LOCK", _SANDBOX + "/ap.lock")

from solvio.autopilot import boundaries as BD   # noqa: E402
from solvio.autopilot import builders as B      # noqa: E402
from solvio.autopilot import contract as C      # noqa: E402
from solvio.autopilot import driver as D        # noqa: E402
from solvio.autopilot import machine as M       # noqa: E402
from solvio.autopilot import status as ST       # noqa: E402
from solvio.autopilot import store as S         # noqa: E402

from test_autopilot_driver import BASIS, _Lead, _arbeitsbereich  # noqa: E402


def _welt(*, adapters=None, urteile=None):
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    led.create_milestone(C.parse(BASIS))
    werk = _arbeitsbereich()
    fahrer = D.Driver(led, workspace=werk,
                      adapters=adapters or {"a": B.SyntheticBuilder()},
                      lead=_Lead(urteile or []), python=sys.executable)
    return led, fahrer, werk, pfad


# ------------------------------------------------------------- Nutzergrenzen
def t_every_boundary_kind_has_a_category() -> None:
    """Eine Grenze ohne Kategorie waere eine Meldung ohne Dringlichkeit."""
    for art in BD.KINDS:
        kategorie = BD.category_for(art)
        require(kategorie in BD.CATEGORIES,
                f"{art} hat die unbekannte Kategorie {kategorie}")
    require_equal(BD.category_for(BD.RELEASE_AUTHORITY), BD.AUTHORITY_REQUIRED,
                  "die Freigabe ist keine Autoritaetsgrenze")
    require_equal(BD.category_for(BD.CONTRACT_CHANGE), BD.DECISION_REQUIRED,
                  "eine Contract-Aenderung ist keine Entscheidung")
    require_raises(ValueError, BD.category_for, "ausgedacht")


def t_the_copied_boundary_kinds_still_match_the_runtime() -> None:
    """Abgeschriebene Namen driften. Diese Zusicherung ist der Preis dafuer,
    dass `autopilot/boundaries.py` die Agentenlaufzeit nicht beim Laden zieht.

    Der Import stuende dem Core im Weg — er muss ohne die Laufzeit hochkommen
    (`t_no_core_module_imports_the_agent_runtime`). Also stehen die Namen als
    Zeichenketten da, und hier wird gegen das Original geprueft.
    """
    from solvio.agent_runtime import boundaries as AB
    require_equal(sorted(BD.INHERITED_KINDS), sorted(AB.BOUNDARY_KINDS),
                  "die abgeschriebenen Grenzarten sind von der Laufzeit "
                  "abgedriftet")
    for art in AB.BOUNDARY_KINDS:
        require(art in BD.CATEGORY_OF,
                f"die Laufzeitart {art} hat im Autopiloten keine Kategorie")


def t_the_things_that_are_never_a_boundary_are_written_down() -> None:
    """Der Auftrag verlangt das woertlich — und eine Liste, die niemand lesen
    kann, ist keine."""
    for wort in ("roter Test", "Kontingent erschoepft", "Builderwechsel"):
        require(any(wort in eintrag for eintrag in BD.NEVER_A_BOUNDARY),
                f"{wort!r} fehlt in der Liste")


def t_a_boundary_reaches_the_one_inbox() -> None:
    """Kein zweiter Posteingang. Und ein Fehlschlag wird nicht verschwiegen."""
    class _Store:
        def __init__(self, ok=True):
            self.items, self.ok = [], ok

        async def add_item(self, item):
            self.items.append(item)
            return self.ok

    store = _Store()
    frage = BD.Ask("probe", BD.DECISION_REQUIRED, "product_decision",
                   "Welche der beiden Formen?")
    gesendet = asyncio.run(BD.notify(store, frage))
    require(gesendet, "die Meldung kam nicht an")
    require_equal(len(store.items), 1, "es wurde nicht genau eine Zeile gelegt")
    require("Welche der beiden Formen" in json.dumps(store.items[0],
                                                     ensure_ascii=False),
            "die Frage fehlt in der Meldung")

    stumm = _Store(ok=False)
    require(not asyncio.run(BD.notify(stumm, frage)),
            "ein abgelehnter Posteingang meldete Erfolg")


def t_answering_the_last_boundary_resumes_exactly_where_it_stopped() -> None:
    """Resume heisst „genau dort weiter" — und erst, wenn ALLE Fragen
    beantwortet sind."""
    led, fahrer, werk, _ = _welt()
    M.transition(led, "probe-a4", S.BUILDING)
    eins = M.park(led, "probe-a4", category=BD.DECISION_REQUIRED,
                  kind="product_decision", question="Erste Frage?")
    zwei = led.open_boundary("probe-a4", category=BD.DECISION_REQUIRED,
                             kind="product_decision", question="Zweite Frage?")

    led.resolve_boundary(eins, "A")
    exc = require_raises(M.TransitionRefused, M.transition, led, "probe-a4",
                         S.BUILDING)
    require_equal(exc.reason, "boundary_still_open",
                  "der Lauf ging mit offener Frage weiter")

    led.resolve_boundary(zwei, "B")
    M.transition(led, "probe-a4", S.BUILDING)
    require_equal(led.milestone("probe-a4").state, S.BUILDING,
                  "der Lauf kehrte nicht an seinen Platz zurueck")


# --------------------------------------------------------------- kill -9
def t_a_killed_driver_loses_nothing_and_claims_nothing() -> None:
    """Der harte Fall: SIGKILL mitten in der Bauphase.

    Geprueft wird beides — dass nichts verloren geht UND dass die
    unterbrochene Phase nicht still zum Erfolg wird.

    Das Bereitschaftssignal ist eine DATEI, nicht die Standardausgabe: der
    Kindprozess protokolliert dorthin, und ein Signal, das sich eine Leitung
    mit Logzeilen teilt, ist kein Signal. (Genau daran haengt diese Suite beim
    ersten Anlauf.)
    """
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    werk = _arbeitsbereich()
    marke = tempfile.mkdtemp(dir=_SANDBOX) + "/BEREIT"
    vertrag = json.dumps(C.parse(BASIS).as_dict())
    quelle = os.path.join(os.path.dirname(__file__), "..", "src")

    skript = f"""
import json, os, sys, time
sys.path.insert(0, {quelle!r})
from solvio.autopilot import contract as C, store as S
led = S.AutopilotLedger({pfad!r})
led.create_milestone(C.parse(json.loads({vertrag!r})))
led.set_fields("probe-a4", last_commit="c-vor-dem-tod", builder="a")
led.record_evidence("probe-a4", kind="test_report", commit="c-vor-dem-tod",
                    env_fingerprint="fp", ok=True, summary="gruen")
led.open_finding("probe-a4", severity="major", title="offen geblieben")
led.start_phase("probe-a4", kind="build", builder="a")
open({marke!r}, "w").write("ja")
time.sleep(120)
"""
    kind = subprocess.Popen([sys.executable, "-c", skript],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        frist = time.time() + 30
        while not os.path.exists(marke) and time.time() < frist:
            require(kind.poll() is None,
                    f"das Kind starb vor der Bereitschaft (rc={kind.returncode})")
            time.sleep(0.1)
        require(os.path.exists(marke), "das Kind wurde nicht bereit")
        os.kill(kind.pid, signal.SIGKILL)      # nicht SIGTERM. Kein Aufraeumen.
        kind.wait(timeout=15)
    finally:
        if kind.poll() is None:
            kind.kill()
            kind.wait(timeout=10)
    require(kind.returncode != 0, "das Kind endete zu freundlich")

    # Ein frischer Prozess uebernimmt.
    led = S.AutopilotLedger(pfad)
    zustand = led.milestone("probe-a4")
    require_equal(zustand.last_commit, "c-vor-dem-tod", "der Commit ging verloren")
    require_equal(zustand.contract_hash, C.parse(BASIS).digest(),
                  "der Contract-Hash driftete")
    require_equal(len(led.findings("probe-a4")), 1, "das Finding ging verloren")
    require_equal(len(led.open_phases("probe-a4")), 1,
                  "die offene Phase fehlt — der Ledger hat den Absturz verschluckt")
    belege = [e for e in led.events("probe-a4") if e["kind"] == "evidence_recorded"]
    require(belege, "die Evidence ging verloren")

    fahrer = D.Driver(led, workspace=werk, adapters={"a": B.SyntheticBuilder()},
                      lead=_Lead([]), python=sys.executable)
    geschlossen = fahrer.reconcile("probe-a4")
    require_equal(len(geschlossen), 1, "die Abstimmung fand die Phase nicht")
    zeile = [p for p in led.phases("probe-a4") if p["phase_id"] == geschlossen[0]][0]
    require_equal(zeile["state"], "interrupted",
                  f"die unterbrochene Phase wurde {zeile['state']} verbucht")
    arten = [e["kind"] for e in led.events("probe-a4")]
    require("interrupted" in arten and "recovered" in arten,
            f"die Chronik verschweigt den Absturz: {arten}")

    # Und der frische Treiber laeuft danach wirklich weiter.
    require_equal(led.milestone("probe-a4").state, S.PLANNING,
                  "der Zustand wurde durch die Abstimmung veraendert")


def t_two_drivers_cannot_run_at_once() -> None:
    """Zwei Treiber am selben Arbeitsbereich waeren zwei Meinungen darueber,
    was dort gerade passiert."""
    sperre = D._open_lock()
    try:
        require_raises(D.DriverLocked, D._open_lock)
    finally:
        sperre.close()
    wieder = D._open_lock()      # nach dem Schliessen geht es wieder
    wieder.close()


# ----------------------------------------------------------------- Status
def t_status_reports_counts_not_percentages() -> None:
    """„7/10 bewiesen" ist eine Auskunft. „73 %" ist eine Erfindung."""
    led, fahrer, werk, _ = _welt()
    bericht = ST.snapshot(led, "probe-a4")
    for feld in ("acceptance_proven", "acceptance_total", "state", "milestone"):
        require(feld in bericht, f"{feld} fehlt im Bericht")
    require(isinstance(bericht["acceptance_proven"], int), "keine Zahl")
    text = json.dumps(bericht, ensure_ascii=False)
    require("%" not in text, f"im Bericht steht ein Prozentwert: {text[:200]}")
    require(bericht["last_gate"] is None,
            "ohne Messung wurde ein Gate-Ergebnis behauptet")


def t_status_shows_the_open_question_and_how_to_answer_it() -> None:
    led, fahrer, werk, _ = _welt()
    M.transition(led, "probe-a4", S.BUILDING)
    bid = M.park(led, "probe-a4", category=BD.AUTHORITY_REQUIRED,
                 kind="mfa_code", question="Bitte den Code aus der App.")
    bericht = ST.snapshot(led, "probe-a4")
    require(bericht["human_required"], "die offene Frage fehlt im Status")
    eintrag = bericht["human_required"][0]
    require_equal(eintrag["id"], bid, "falsche Grenz-Kennung")
    require_equal(eintrag["category"], BD.AUTHORITY_REQUIRED, "falsche Kategorie")


def t_the_probe_distinguishes_waiting_from_healthy() -> None:
    """Ein Milestone, der auf den Menschen wartet, ist nicht gesund — und
    einer, der auf ein Kontingent wartet, auch nicht. Beide sind aber auch
    nicht kaputt."""
    led, fahrer, werk, _ = _welt()
    wort, _grund = ST.assess(led)
    require_equal(wort, "healthy", f"ein frischer Milestone war nicht gesund: {wort}")

    M.transition(led, "probe-a4", S.BUILDING)
    M.park(led, "probe-a4", category=BD.DECISION_REQUIRED,
           kind="product_decision", question="Welche Form?")
    wort, grund = ST.assess(led)
    require_equal(wort, "auth_required", f"das Warten auf den Menschen: {wort}")
    require("probe-a4" in grund, "der Grund nennt den Milestone nicht")

    led2 = S.AutopilotLedger(tempfile.mkdtemp(dir=_SANDBOX) + "/b.sqlite3")
    led2.create_milestone(C.parse(BASIS))
    M.transition(led2, "probe-a4", S.BUILDING)
    M.transition(led2, "probe-a4", S.BLOCKED, block_reason="capacity")
    wort, grund = ST.assess(led2)
    require_equal(wort, "degraded", f"das Warten auf ein Kontingent: {wort}")
    require("capacity" in grund, "der Grund nennt das Kontingent nicht")


def t_an_unreadable_ledger_is_not_healthy() -> None:
    """Fail closed, auch in der Probe."""
    class _Kaputt:
        def milestones(self, **_k):
            raise RuntimeError("Datei weg")

    wort, grund = ST.assess(_Kaputt())
    require(wort in ("unavailable", "unknown"),
            f"ein unlesbares Buch galt als {wort}")


def t_the_cli_answers_a_boundary_and_resumes() -> None:
    """Der Weg des Eigentuemers zurueck — ueber das echte Skript."""
    led, fahrer, werk, pfad = _welt()
    M.transition(led, "probe-a4", S.BUILDING)
    bid = M.park(led, "probe-a4", category=BD.DECISION_REQUIRED,
                 kind="product_decision", question="Welche Form?")
    led.close()

    umgebung = {**os.environ, "SOLVIO_AUTOPILOT_DB": pfad}
    skript = os.path.join(os.path.dirname(__file__), "..", "scripts", "autopilot.py")
    proc = subprocess.run([sys.executable, skript, "answer", "--boundary", bid,
                           "--text", "Nimm die erste."], env=umgebung,
                          capture_output=True, text=True, timeout=60)
    require_equal(proc.returncode, 0, f"die CLI scheiterte: {proc.stderr[-300:]}")
    require("laeuft weiter bei BUILDING" in proc.stdout,
            f"die CLI meldete keinen Fortlauf: {proc.stdout[-200:]}")

    wieder = S.AutopilotLedger(pfad)
    require_equal(wieder.milestone("probe-a4").state, S.BUILDING,
                  "der Milestone lief nicht weiter")
    require(not wieder.open_boundaries("probe-a4"), "die Grenze blieb offen")


# ------------------------------------------------- die neue Kontrolloperation
def t_the_token_operation_only_mints_the_two_autopilot_principals() -> None:
    """Eine Token-Operation mit freiem Namen waere ein Token-Automat.

    Sie koennte den Token des Deep-Gateways oder eines fremden
    Eskalations-Auftraggebers praegen. Deshalb ist die Namensliste geschlossen
    — und diese Zusicherung stellt sie.
    """
    from solvio.realtime import control as CTRL

    require_equal(sorted(CTRL.AUTOPILOT_PRINCIPALS),
                  ["autopilot-lead", "autopilot-lead-escalation",
                   "autopilot-writer-claude",
                   "autopilot-writer-claude-escalation"],
                  f"unerwartete Namensliste: {CTRL.AUTOPILOT_PRINCIPALS}")
    require(CTRL.AUTOPILOT_TOKEN in CTRL.OPERATIONS,
            "die Operation ist nicht registriert")

    class _Broker:
        def __init__(self):
            self.gefragt = []

        def register_principal(self, name):
            self.gefragt.append(name)
            return f"solvio-broker-{name}"

    class _Dispatcher:
        pass

    disp = _Dispatcher()
    disp.provider_broker = _Broker()
    steuerung = CTRL.CoreControl(disp, socket_path=_SANDBOX + "/ctrl.sock")

    # Erlaubt
    antwort = asyncio.run(steuerung.handle(
        {"op": CTRL.AUTOPILOT_TOKEN, "principal": "autopilot-lead"}))
    require(antwort.get("ok"), f"der erlaubte Name wurde abgewiesen: {antwort}")
    require(antwort["token"].startswith("solvio-broker-"), "kein Token")

    # Verboten — jeder andere Auftraggeber
    for fremd in ("deep-gateway", "agent-runtime-escalation",
                  "cognitive-router-escalation", "bot:solvio-researcher", ""):
        antwort = asyncio.run(steuerung.handle(
            {"op": CTRL.AUTOPILOT_TOKEN, "principal": fremd}))
        require(not antwort.get("ok"),
                f"der fremde Auftraggeber {fremd!r} bekam einen Token")
    require_equal(disp.provider_broker.gefragt, ["autopilot-lead"],
                  f"der Broker wurde fuer fremde Namen gefragt: "
                  f"{disp.provider_broker.gefragt}")


def t_the_lease_operation_serves_only_the_two_named_principals() -> None:
    """Ein Lease ist der Schluessel, den der Token allein nicht dreht.

    Deshalb traegt dieser Vorgang dieselbe geschlossene Namensliste wie der
    Token. Waere der Name frei, koennte sich ein Anrufer ein Lease auf dem
    Deep-Gateway oeffnen und dessen Kappe verbrauchen.
    """
    from solvio.realtime import control as CTRL

    require(CTRL.AUTOPILOT_LEASE in CTRL.OPERATIONS,
            "die Lease-Operation ist nicht registriert")
    require_equal(sorted(CTRL.LEASE_ACTIONS), ["close", "open"],
                  f"unerwartete Aktionsliste: {CTRL.LEASE_ACTIONS}")

    class _Broker:
        def __init__(self):
            self.offen, self.geschlossen, self.fristen = [], [], []

        def open_lease(self, principal, ref, *, deadline):
            self.offen.append((principal, ref))
            self.fristen.append(deadline)
            return f"lease-{len(self.offen)}"

        def close_lease(self, lease_id):
            self.geschlossen.append(lease_id)

    class _Dispatcher:
        pass

    disp = _Dispatcher()
    disp.provider_broker = _Broker()
    steuerung = CTRL.CoreControl(disp, socket_path=_SANDBOX + "/lease.sock")

    antwort = asyncio.run(steuerung.handle(
        {"op": CTRL.AUTOPILOT_LEASE, "principal": "autopilot-lead",
         "action": "open"}))
    require(antwort.get("ok"), f"das erlaubte Lease wurde abgewiesen: {antwort}")
    require(antwort["lease_id"].startswith("lease-"), "keine Lease-Kennung")

    for fremd in ("deep-gateway", "agent-runtime-escalation",
                  "bot:solvio-researcher", ""):
        antwort = asyncio.run(steuerung.handle(
            {"op": CTRL.AUTOPILOT_LEASE, "principal": fremd, "action": "open"}))
        require(not antwort.get("ok"),
                f"der fremde Auftraggeber {fremd!r} bekam ein Lease")

    for unfug in ("delete", "extend", "steal", ""):
        antwort = asyncio.run(steuerung.handle(
            {"op": CTRL.AUTOPILOT_LEASE, "principal": "autopilot-lead",
             "action": unfug}))
        require(not antwort.get("ok"),
                f"die erfundene Aktion {unfug!r} wurde ausgefuehrt")

    require_equal([p for p, _ in disp.provider_broker.offen], ["autopilot-lead"],
                  f"der Broker wurde fuer fremde Namen gefragt: "
                  f"{disp.provider_broker.offen}")


def t_the_caller_cannot_set_the_lease_deadline() -> None:
    """Eine Frist, die der Bittsteller bestimmt, ist keine."""
    from solvio.realtime import control as CTRL

    class _Broker:
        def __init__(self):
            self.fristen = []

        def open_lease(self, principal, ref, *, deadline):
            self.fristen.append(deadline)
            return "lease-1"

        def close_lease(self, lease_id):
            pass

    class _Dispatcher:
        pass

    disp = _Dispatcher()
    disp.provider_broker = _Broker()
    steuerung = CTRL.CoreControl(disp, socket_path=_SANDBOX + "/lease2.sock")
    vorher = time.time()
    asyncio.run(steuerung.handle(
        {"op": CTRL.AUTOPILOT_LEASE, "principal": "autopilot-lead",
         "action": "open", "deadline": vorher + 86400,
         "lease_seconds": 86400}))
    frist = disp.provider_broker.fristen[0]
    require(frist - vorher <= CTRL.LEASE_SECONDS + 5,
            f"der Anrufer hat die Frist gesetzt: {frist - vorher:.0f} s statt "
            f"hoechstens {CTRL.LEASE_SECONDS:.0f} s")


def t_the_token_operation_fails_closed_without_a_broker() -> None:
    """Ohne Broker gibt es keinen Token — und keinen Absturz."""
    from solvio.realtime import control as CTRL

    class _Dispatcher:
        pass

    steuerung = CTRL.CoreControl(_Dispatcher(), socket_path=_SANDBOX + "/c2.sock")
    antwort = asyncio.run(steuerung.handle(
        {"op": CTRL.AUTOPILOT_TOKEN, "principal": "autopilot-lead"}))
    require(not antwort.get("ok"), "ohne Broker kam ein Token")


def t_a_missing_core_is_a_situation_not_a_crash() -> None:
    """Laeuft der Core nicht, ist das eine Lage. Der Treiber stuerzt nicht ab."""
    from solvio.autopilot.lead import token_from_core
    require_equal(token_from_core("autopilot-lead",
                                  socket_path=_SANDBOX + "/gibtesnicht.sock"),
                  "", "ein fehlender Core lieferte etwas")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
