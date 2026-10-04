"""Autopilot A5 — Failover und Anti-Loop.

Die eine Unterscheidung, die diese Suite haelt und die der Abschlussbericht
ebenfalls halten muss:

**FAILOVER ENGINE** — die Mechanik: Kontingent erschoepft, sicherer Checkpoint,
zweiter Adapter uebernimmt, Arbeit geht weiter. Das wird hier bewiesen.

**REAL CLAUDE↔CODEX WRITER FAILOVER** — zwei reale Schreiber. Das wird hier
NICHT bewiesen und existiert in V0.5 nicht: `builder/claude` ist aus
gemessenen Sicherheitsgruenden gesperrt (Amendment 2). Ein kontrollierter
zweiter Adapter beweist die Mechanik; er ist kein zweiter Writer.

Und die zweite Zusicherung: **Kontingent ist kein Reparaturversuch.** Wer es
mitzaehlt, schickt einen Milestone nach zwei Kontingentenden zur
Ursachenanalyse, obwohl niemand etwas falsch gemacht hat.

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

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a5-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ.setdefault("SOLVIO_AUTOPILOT_LOCK", _SANDBOX + "/ap.lock")

from solvio.autopilot import builders as B     # noqa: E402
from solvio.autopilot import capacity as CAP    # noqa: E402
from solvio.autopilot import contract as C     # noqa: E402
from solvio.autopilot import driver as D       # noqa: E402
from solvio.autopilot import lead as LEAD      # noqa: E402
from solvio.autopilot import store as S        # noqa: E402

from test_autopilot_driver import (BASIS, GATE_ROT, _Lead,  # noqa: E402
                                   _arbeitsbereich, _git)


def _welt(*, adapters, urteile=None):
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    led.create_milestone(C.parse(BASIS))
    werk = _arbeitsbereich()
    lead = _Lead(urteile or [{"verdict": "FIX", "next_action": "fix",
                              "rationale": "weiter", "task": "repariere"}])
    fahrer = D.Driver(led, workspace=werk, adapters=adapters, lead=lead,
                      python=sys.executable)
    return led, fahrer, lead, werk


# ------------------------------------------------------------ Failover-Engine
def t_quota_hands_over_to_the_next_builder_and_work_continues() -> None:
    """Die Mechanik, am Stueck: A erschoepft → Checkpoint → B baut weiter.

    Wichtig ist nicht nur, DASS B uebernimmt, sondern dass A vorher einen
    sauberen Punkt hinterlaesst und die Uebernahme im Buch steht.
    """
    erschoepft = B.SyntheticBuilder(outcome=B.QUOTA)
    uebernehmer = B.SyntheticBuilder(writes={"weiter.txt": "B war hier\n"})
    led, fahrer, lead, werk = _welt(
        adapters={"a_erschoepft": erschoepft, "b_frisch": uebernehmer})

    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    require_equal(erschoepft.calls, 1, "der erste Builder lief nicht")
    require_equal(uebernehmer.calls, 1,
                  f"der zweite Builder uebernahm nicht ({uebernehmer.calls}x)")
    require(os.path.isfile(os.path.join(werk, "weiter.txt")),
            "die Arbeit des Uebernehmers fehlt")

    arten = [e["kind"] for e in led.events("probe-a4")]
    require("quota_failover" in arten, f"kein Failover-Ereignis: {arten}")
    require("builder_switched" in arten, "der Wechsel steht nicht im Buch")

    stand = subprocess.run(["git", "-C", werk, "log", "--oneline"],
                           capture_output=True, text=True).stdout
    require("autopilot-checkpoint" in stand,
            f"es wurde kein Checkpoint gesetzt: {stand[:200]}")
    require_equal(led.milestone("probe-a4").builder, "b_frisch",
                  "das Buch nennt den falschen Builder")


def t_quota_is_not_counted_as_a_repair_attempt() -> None:
    """Der Kern der Unterscheidung.

    Zweimal Kontingent darf die Schleifenbremse NICHT ausloesen — sonst
    verwandelt ein knappes Abo eine gesunde Arbeit in eine Ursachenanalyse.
    """
    led, fahrer, lead, _ = _welt(
        adapters={"a": B.SyntheticBuilder(outcome=B.QUOTA),
                  "b": B.SyntheticBuilder(writes={"x.txt": "1\n"})})
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    quota_phasen = [p for p in led.phases("probe-a4") if p["state"] == "quota"]
    require(quota_phasen, "keine Quota-Phase verbucht")
    for p in quota_phasen:
        require_equal(fahrer.failed_attempts("probe-a4", p["attempt_digest"]), 0,
                      "ein Kontingent wurde als Reparaturversuch gezaehlt")
    require(not fahrer.loop_detected("probe-a4", quota_phasen[0]["attempt_digest"]),
            "zwei Kontingente loesten die Schleifenbremse aus")


def t_when_no_writer_is_left_the_milestone_blocks_and_wakes_itself() -> None:
    """Kein zugelassener Builder heisst BLOCKED — nicht HUMAN_REQUIRED.

    Der Eigentuemer wird nicht geweckt, weil ein Kontingent endete. Der
    Milestone wartet und weckt sich selbst.
    """
    led, fahrer, lead, _ = _welt(
        adapters={"a": B.SyntheticBuilder(outcome=B.QUOTA),
                  "b": B.SyntheticBuilder(outcome=B.QUOTA)})
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.BLOCKED, f"falscher Zustand: {zustand}")
    eintrag = led.milestone("probe-a4")
    require_equal(eintrag.block_reason, "capacity", "falscher Grund")
    require(eintrag.resume_at > 0, "es wurde kein Weckzeitpunkt gesetzt")
    require(not led.open_boundaries("probe-a4"),
            "ein Kontingentende hat den Menschen gerufen")


def t_a_blocked_milestone_resumes_when_the_wake_time_arrives() -> None:
    """Der Weckzeitpunkt ist kein Schmuck — er fuehrt zurueck."""
    led, fahrer, lead, _ = _welt(
        adapters={"a": B.SyntheticBuilder(outcome=B.QUOTA)})
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(led.milestone("probe-a4").state, S.BLOCKED, "nicht blockiert")

    # Jetzt ist wieder Kontingent da, und die Weckzeit ist erreicht.
    fahrer.adapters = {"a": B.SyntheticBuilder(writes={"spaeter.txt": "ok\n"})}
    weckzeit = led.milestone("probe-a4").resume_at
    asyncio.run(fahrer.run("probe-a4", max_rounds=2, now=weckzeit + 1))
    require(led.milestone("probe-a4").state not in (S.BLOCKED,),
            "der Milestone wachte nicht auf")


def t_a_blocked_milestone_stays_asleep_before_its_wake_time() -> None:
    """Die Gegenprobe: vorher wird nicht aufgewacht, sonst waere die Wartezeit
    eine Behauptung."""
    led, fahrer, lead, _ = _welt(
        adapters={"a": B.SyntheticBuilder(outcome=B.QUOTA)})
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    weckzeit = led.milestone("probe-a4").resume_at
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=2,
                                     now=weckzeit - 100))
    require_equal(zustand, S.BLOCKED, "der Milestone wachte zu frueh auf")


def t_a_blocked_writer_is_skipped_for_its_security_reason() -> None:
    """Der gesperrte Claude-Adapter wird uebersprungen — und der Grund steht
    fest, damit der Bericht ihn spaeter nennen kann."""
    led, fahrer, lead, _ = _welt(
        adapters={"claude": B.ClaudeBuilder(),
                  "b": B.SyntheticBuilder(writes={"y.txt": "1\n"})})
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(led.milestone("probe-a4").builder, "b",
                  "der gesperrte Adapter hat gebaut")
    lage = led.capacity("probe-a4")
    require(S.ROLE_BUILDER in lage, "keine Builder-Capacity gebucht")


def t_the_switch_event_names_the_builder_that_left() -> None:
    """Eine Zeile, die das Gegenteil sagt, liest spaeter niemand richtig.

    Live gelesen in der V0.6-Abnahme: im Buch stand `builder_switched: nach
    claude`, waehrend Codex uebernahm. `name` ist an dieser Stelle der
    ERSCHOEPFTE Builder, nicht der neue.
    """
    erschoepft = B.SyntheticBuilder(outcome=B.QUOTA, detail="Kontingent")
    erschoepft.name = "aaa-erschoepft"
    fertig = B.SyntheticBuilder(writes={"x.txt": "1\n"})
    led, fahrer, _lead, _ = _welt(adapters={"aaa-erschoepft": erschoepft,
                                            "synthetic": fertig},
                                  urteile=[{"verdict": "FIX",
                                            "next_action": "fix",
                                            "rationale": "x"}])
    asyncio.run(fahrer._build_phase("probe-a4"))
    wechsel = [e["summary"] for e in led.events("probe-a4")
               if e["kind"] == "builder_switched"]
    require(wechsel, "es wurde kein Wechsel gebucht")
    require("weg von aaa-erschoepft" in wechsel[0],
            f"der Wechsel nennt nicht den, der ging: {wechsel}")
    require("nach aaa-erschoepft" not in wechsel[0],
            f"der Wechsel liest sich, als habe der Erschoepfte uebernommen: "
            f"{wechsel}")


def t_a_second_writer_is_permitted_but_not_yet_proven() -> None:
    """Die Ehrlichkeitszusicherung fuer den Abschlussbericht.

    **Was sich mit V0.6 geaendert hat.** In V0.5 gab es genau EINEN realen
    Schreiber, und diese Zusicherung hielt das fest. Seit V0.6 fuehrt die
    Registratur zwei — aber „erlaubt" und „bewiesen" sind zwei verschiedene
    Aussagen, und der Bericht darf sie nicht verwechseln.

    Geprueft wird deshalb die Trennung selbst:

    * `writers()` ist eine RICHTLINIENaussage: wer schreiben darf.
    * Ob einer gerade kann, steht in `capacity.py` — und ohne Anmeldung sagt
      der gemakelte Claude ehrlich `no_credential`.
    * Der synthetische Builder gehoert in keine der beiden Antworten.

    Ein bidirektionaler Schreib-Failover ist damit noch NICHT bewiesen. Das
    beweist erst ein Lauf mit zwei echten Schreibern (B5) — nicht diese Liste.
    """
    echte = B.default_adapters()
    require_equal(B.writers(echte), ["claude", "codex"],
                  f"unerwartete Menge erlaubter Schreiber: {B.writers(echte)}")
    require(B.SyntheticBuilder().name not in echte,
            "der synthetische Builder steht in der echten Registratur")

    # Erlaubt heisst nicht einsatzbereit: ohne Anmeldung nennt er den Grund.
    claude = echte["claude"]
    require_equal(claude.capability(), B.WRITER,
                  "der gemakelte Schreiber ist grundsaetzlich gesperrt")
    grund = claude.blocked_reason()
    require(grund in ("", "no_credential") or grund.startswith("cli_canary_"),
            f"unerwarteter Grund am zweiten Schreiber: {grund}")

    # Und der Rueckfall ohne Anthropic-Flaeche fuehrt weiterhin genau einen.
    alt = B.default_adapters(brokered_claude=False)
    require_equal(B.writers(alt), ["codex"],
                  f"der Rueckfall fuehrt mehr als einen: {B.writers(alt)}")


# ---------------------------------------------------------------- Anti-Loop
def t_two_identical_failed_attempts_stop_the_normal_retry() -> None:
    """Nach zwei erfolglosen gleichartigen Versuchen ist Schluss mit „nochmal".

    Der Lead schlaegt ein drittes Mal dasselbe vor; der Treiber haelt an,
    statt es zu tun.
    """
    versager = B.SyntheticBuilder(outcome=B.FAILED, detail="geht nicht")
    led, fahrer, lead, _ = _welt(
        adapters={"a": versager},
        urteile=[{"verdict": "FIX", "next_action": "fix",
                  "rationale": "nochmal", "task": "repariere"}] * 4)
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=4))

    arten = [e["kind"] for e in led.events("probe-a4")]
    require("loop_detected" in arten, f"die Schleife wurde nicht erkannt: {arten}")
    require_equal(zustand, S.HUMAN_REQUIRED,
                  f"der Treiber lief weiter statt anzuhalten: {zustand}")
    grenzen = led.open_boundaries("probe-a4")
    require(grenzen and "drittes Mal" in grenzen[0]["question"],
            "die Grenze erklaert nicht, warum angehalten wurde")


def t_the_loop_brake_counts_failures_not_rounds() -> None:
    """Verschiedene Aufgaben sind keine Schleife.

    Eine Bremse, die Runden zaehlt statt gleichartiger Fehlversuche, wuerde
    gesunden Fortschritt abwuergen.
    """
    led, fahrer, lead, _ = _welt(adapters={"a": B.SyntheticBuilder()})
    eins = fahrer._digest("probe-a4", "a", "repariere modul_x")
    zwei = fahrer._digest("probe-a4", "a", "baue modul_y")
    require(eins != zwei, "verschiedene Aufgaben ergaben denselben Digest")

    for _ in range(2):
        pid = led.start_phase("probe-a4", kind="build", builder="a",
                              attempt_digest=eins)
        led.finish_phase(pid, state="failed", summary="rot")
    require(fahrer.loop_detected("probe-a4", eins),
            "zwei gleiche Fehlversuche loesten nicht aus")
    require(not fahrer.loop_detected("probe-a4", zwei),
            "eine andere Aufgabe wurde mitgezaehlt")


def t_a_loop_escalates_the_lead_to_the_large_model() -> None:
    """Wer zweimal dasselbe erfolglos versucht hat, braucht eine andere Sicht.

    Geprueft am Auftraggeber, nicht am Modellnamen: der Name ist keine
    Berechtigung, der Auftraggeber ist es.
    """
    klein = LEAD.transport_for(LEAD.TIER_SMALL)
    gross = LEAD.transport_for(LEAD.TIER_LARGE)
    require(klein[0] != gross[0], "beide Stufen benutzen denselben Auftraggeber")
    require_equal(gross[1], "gpt-5.4", f"falsches grosses Modell: {gross[1]}")

    from solvio.provider_broker import session as SE
    kappen = SE._default_caps(gross[0])
    require(kappen.allowed_models == frozenset({"gpt-5.4"}),
            f"der Eskalations-Auftraggeber ist nicht auf ein Modell begrenzt: "
            f"{kappen.allowed_models}")
    normal = SE._default_caps(klein[0])
    require(not normal.allowed_models or "gpt-5.4" not in normal.allowed_models,
            "der normale Lead-Auftraggeber erreicht das grosse Modell")


# ------------------------------------------------------- der Lease-Befund
def t_the_lead_opens_a_lease_before_it_calls() -> None:
    """Live gelernt, und es kostete 42 Minuten.

    In der ersten A7-Abnahme baute Codex 30 Minuten, das Gate mass 12 Minuten
    — und der erste Review-Aufruf endete an `broker.denied reason=lease_absent
    status=403`. Ein Broker-Token allein oeffnet nichts; das ist der Zweck des
    Leases und nicht sein Zubehoer.

    Geprueft am Verhalten: der Lead MUSS vor dem Aufruf ein Lease oeffnen und
    es danach wieder schliessen, auch wenn der Aufruf scheitert.
    """
    class _Broker:
        def __init__(self):
            self.offen, self.geschlossen = [], []

        def register_principal(self, name):
            return f"tok-{name}"

        def open_lease(self, principal, ref, *, deadline):
            self.offen.append((principal, ref))
            return f"lease-{len(self.offen)}"

        def close_lease(self, lease_id):
            self.geschlossen.append(lease_id)

    broker = _Broker()
    chef = LEAD.TechnicalLead(broker=broker, port=1)

    async def _antwort(payload, *, token):
        return {"ok": True, "text": json.dumps(
            {"verdict": "FIX", "next_action": "fix", "rationale": "x"}),
            "tokens": 10}

    chef._call = _antwort
    urteil = asyncio.run(chef.judge(
        context="lage", allowed_builders=["codex"], open_finding_ids=set(),
        criterion_keys=set(), evidence_ids=set(), deterministic_keys=set()))
    require_equal(urteil.verdict, "FIX", "das Urteil kam nicht durch")
    require_equal(len(broker.offen), 1,
                  f"es wurde kein Lease geoeffnet: {broker.offen}")
    require_equal(len(broker.geschlossen), 1,
                  "das Lease wurde nicht geschlossen")


def t_a_stray_escalation_field_costs_the_field_not_the_verdict() -> None:
    """Ohne ESCALATE bedeutet das Feld nichts — also darf es nichts kosten.

    Live gemessen im Produktions-Smoke am 2026-09-02: der Lead schickte zu
    einem FIX-Urteil `escalation_event: "NONE"`, und die Pruefung verwarf das
    ganze Urteil. Es war nichts daran falsch ausser der Strenge an der
    falschen Stelle — das Schema zeigt das Feld an, und "NONE" heisst genau
    „ich eskaliere nicht".
    """
    for wert in ("NONE", "KEINES", "n/a", ""):
        roh = json.dumps({"verdict": "FIX", "next_action": "fix",
                          "rationale": "x", "escalation_event": wert})
        urteil = LEAD.validate(roh, allowed_builders={"codex"},
                               open_finding_ids=set(), criterion_keys=set(),
                               evidence_ids=set(), deterministic_keys=set())
        require_equal(urteil.verdict, "FIX",
                      f"das Urteil fiel an escalation_event={wert!r}")
        require_equal(urteil.escalation_event, "",
                      f"ein bedeutungsloses Ereignis blieb stehen: "
                      f"{urteil.escalation_event!r}")


def t_escalate_still_needs_a_real_event() -> None:
    """Die Gegenprobe: dort ist die Strenge der ganze Zweck.

    Ohne sie koennte sich jede Runde zum teuren Modell hochreden.
    """
    for wert in ("NONE", "WEIL_ICH_ES_WILL", ""):
        roh = json.dumps({"verdict": "ESCALATE",
                          "next_action": "model_escalation",
                          "rationale": "x", "escalation_event": wert})
        try:
            LEAD.validate(roh, allowed_builders={"codex"},
                          open_finding_ids=set(), criterion_keys=set(),
                          evidence_ids=set(), deterministic_keys=set())
        except LEAD.LeadRefused as exc:
            require("escalation_without_event" in str(exc),
                    f"falscher Grund fuer {wert!r}: {exc}")
        else:
            raise AssertionError(
                f"ESCALATE kam mit escalation_event={wert!r} durch")

    # Und mit einem echten Ereignis traegt es.
    roh = json.dumps({"verdict": "ESCALATE", "next_action": "model_escalation",
                      "rationale": "x",
                      "escalation_event": "SECOND_FIX_FAILED"})
    urteil = LEAD.validate(roh, allowed_builders={"codex"},
                           open_finding_ids=set(), criterion_keys=set(),
                           evidence_ids=set(), deterministic_keys=set())
    require_equal(urteil.escalation_event, "SECOND_FIX_FAILED",
                  "ein gueltiges Ereignis ging verloren")


def t_without_a_lease_the_lead_never_sends_the_call() -> None:
    """Kein Lease heisst NICHT SENDEN — auch ausserhalb des Core-Prozesses.

    Bis zum 2026-09-02 stand hier eine Ausnahme: „kein Broker im Prozess" hiess
    „ohne Lease losschicken, der Broker wird schon 403 sagen". Ehrlich war das,
    brauchbar nicht — und es war der Grund, warum die A7-Abnahme einen zweiten
    Broker-Prozess brauchte. Jetzt holt der Lead das Lease ueber den
    Kontrollsocket des laufenden Cores; kommt keins, wird nicht gesendet.
    """
    chef = LEAD.TechnicalLead(broker=None, port=1)
    gesendet = []

    async def _antwort(payload, *, token):
        gesendet.append(token)
        return {"ok": True, "text": json.dumps(
            {"verdict": "FIX", "next_action": "fix", "rationale": "x"})}

    chef._call = _antwort
    chef._token = lambda principal: "tok-egal"
    chef._open_lease = lambda principal: ""      # der Core gibt keins

    try:
        asyncio.run(chef.judge(
            context="lage", allowed_builders=["codex"], open_finding_ids=set(),
            criterion_keys=set(), evidence_ids=set(), deterministic_keys=set()))
    except LEAD.LeadRefused as exc:
        require("lease_refused" in str(exc), f"falscher Grund: {exc}")
    else:
        raise AssertionError("der Aufruf ging ohne Lease hinaus")
    require_equal(gesendet, [], f"es wurde trotzdem gesendet: {gesendet}")


def t_outside_the_core_the_lead_asks_the_core_for_its_lease() -> None:
    """Ein zweiter Broker waere die falsche Antwort — zwei Kappen sind keine."""
    chef = LEAD.TechnicalLead(broker=None, port=1)
    gefragt = []

    import solvio.autopilot.lead as MODUL
    alt_open, alt_close = MODUL.lease_from_core, MODUL.close_lease_in_core
    MODUL.lease_from_core = lambda p, **k: (gefragt.append(("open", p))
                                            or "lease-core-1")
    MODUL.close_lease_in_core = lambda p, l, **k: gefragt.append(("close", p, l))
    try:
        require_equal(chef._open_lease("autopilot-lead"), "lease-core-1",
                      "das Lease kam nicht aus dem Core")
        chef._close_lease("lease-core-1", "autopilot-lead")
    finally:
        MODUL.lease_from_core, MODUL.close_lease_in_core = alt_open, alt_close

    require_equal(gefragt, [("open", "autopilot-lead"),
                            ("close", "autopilot-lead", "lease-core-1")],
                  f"der Weg ueber den Core wurde nicht genommen: {gefragt}")


def t_a_deterministic_key_costs_the_entry_not_the_verdict() -> None:
    """Ein Modellfehler darf eine Runde nicht kosten, wenn er nichts oeffnet.

    Live gemessen in zwei A7-Laeufen: der Lead nannte `gate` unter `proven` —
    trotz ausdruecklicher Anweisung und trotz ausdruecklicher Nennung im
    Context. Das ganze Urteil dafuer zu verwerfen kostete jedes Mal 30 Minuten
    Bauen und 14 Minuten Messen.

    Der Eintrag faellt jetzt weg, das Urteil bleibt — und das Kriterium bleibt
    offen. Sicherheit unveraendert: nur die Messung schliesst es.
    """
    roh = json.dumps({"verdict": "READY", "next_action": "ready",
                      "rationale": "alles gruen",
                      "findings": [{"severity": "minor", "title": "Rest",
                                    "detail": "kleinigkeit"}],
                      "proven": [{"key": "gate", "evidence_ref": "ev-1"},
                                 {"key": "lesbar", "evidence_ref": "ev-1"}]})
    urteil = LEAD.validate(roh, allowed_builders={"codex"}, open_finding_ids=set(),
                           criterion_keys={"gate", "lesbar"},
                           evidence_ids={"ev-1"},
                           deterministic_keys={"gate"})
    require_equal(urteil.verdict, "READY", "das Urteil wurde verworfen")
    schluessel = [e["key"] for e in urteil.proven]
    require_equal(schluessel, ["lesbar"],
                  f"der DETERMINISTIC-Eintrag ueberlebte: {schluessel}")
    require_equal(len(urteil.findings), 1,
                  "die uebrigen Beobachtungen gingen mit verloren")


def t_a_made_up_criterion_still_destroys_the_verdict() -> None:
    """Die Gegenprobe: nachgiebig heisst nicht nachgiebig ueberall.

    Ein erfundener Schluessel heisst, dass der Lead ueber einen anderen
    Contract spricht. Das ist keine Schluderei an einer Stelle, sondern ein
    Hinweis, dass das ganze Urteil nicht zu dieser Lage gehoert.
    """
    roh = json.dumps({"verdict": "READY", "next_action": "ready",
                      "rationale": "x",
                      "proven": [{"key": "test_gate_green",
                                  "evidence_ref": "ev-1"}]})
    try:
        LEAD.validate(roh, allowed_builders={"codex"}, open_finding_ids=set(),
                      criterion_keys={"gate"}, evidence_ids={"ev-1"},
                      deterministic_keys={"gate"})
    except LEAD.LeadRefused as exc:
        require("unknown_criterion" in str(exc),
                f"falscher Grund: {exc}")
    else:
        raise AssertionError("ein erfundenes Kriterium kam durch")

    # Und eine erfundene Evidence-Kennung ebenso.
    roh2 = json.dumps({"verdict": "READY", "next_action": "ready",
                       "rationale": "x",
                       "proven": [{"key": "lesbar", "evidence_ref": "ev-erfunden"}]})
    try:
        LEAD.validate(roh2, allowed_builders={"codex"}, open_finding_ids=set(),
                      criterion_keys={"lesbar"}, evidence_ids={"ev-1"},
                      deterministic_keys=set())
    except LEAD.LeadRefused as exc:
        require("proven_without_known_evidence" in str(exc),
                f"falscher Grund: {exc}")
    else:
        raise AssertionError("eine erfundene Belegkennung kam durch")


def t_a_second_verdict_survives_the_brokers_token_rotation() -> None:
    """Ein Token gilt nur so lange wie sein Auftrag. Live gelernt, teuer.

    Der Broker rotiert den Token, sobald das letzte Lease eines Auftraggebers
    schliesst — Schicht 3, „kein Zugang ueber den Auftrag hinaus". Der Lead
    merkte sich den Token trotzdem. In der A7-Abnahme gelang der erste
    Review-Aufruf; der zweite endete 45 Minuten spaeter an `broker.denied
    principal=unknown reason=bad_token status=401`, und der Milestone stand
    BLOCKED.

    Die Attrappe hier rotiert deshalb genau so wie der echte Broker. Eine, die
    das nicht tut, prueft die Rotation nicht — und genau daran lag es.
    """
    class _RotierenderBroker:
        def __init__(self):
            self.generation = 0
            self.gueltig = ""
            self.offen = []

        def register_principal(self, name):
            self.generation += 1
            self.gueltig = f"tok-{name}-{self.generation}"
            return self.gueltig

        def open_lease(self, principal, ref, *, deadline):
            self.offen.append(ref)
            return f"lease-{len(self.offen)}"

        def close_lease(self, lease_id):
            # Wie der echte: faellt der Auftraggeber auf null, rotiert er.
            self.generation += 1
            self.gueltig = f"tok-rotiert-{self.generation}"

    broker = _RotierenderBroker()
    chef = LEAD.TechnicalLead(broker=broker, port=1)
    gesehen: list[str] = []

    async def _antwort(payload, *, token):
        gesehen.append(token)
        if token != broker.gueltig:
            return {"ok": False, "reason": "broker_401"}
        return {"ok": True, "text": json.dumps(
            {"verdict": "FIX", "next_action": "fix", "rationale": "x"}),
            "tokens": 10}

    chef._call = _antwort
    for runde in (1, 2, 3):
        urteil = asyncio.run(chef.judge(
            context="lage", allowed_builders=["codex"], open_finding_ids=set(),
            criterion_keys=set(), evidence_ids=set(), deterministic_keys=set()))
        require_equal(urteil.verdict, "FIX",
                      f"Runde {runde} kam nicht durch (Token {gesehen[-1]!r}, "
                      f"gueltig waere {broker.gueltig!r})")
    require_equal(len(set(gesehen)), 3,
                  f"der Lead benutzte denselben Token mehrfach: {gesehen}")

def t_the_token_is_minted_before_the_lease_is_opened() -> None:
    """Reihenfolge, live gemessen im zweiten A7-Lauf.

    Ein Lease auf einen noch nicht registrierten Auftraggeber wird mit
    `CapExceeded` abgewiesen. Danach lief der Aufruf ohne Lease weiter und
    endete im 403 — der Fehler kam spaeter und unklarer, als er musste.
    """
    reihenfolge: list[str] = []

    class _Broker:
        def register_principal(self, name):
            reihenfolge.append("token")
            return "tok"

        def open_lease(self, principal, ref, *, deadline):
            reihenfolge.append("lease")
            return "lease-1"

        def close_lease(self, lease_id):
            reihenfolge.append("close")

    chef = LEAD.TechnicalLead(broker=_Broker(), port=1)

    async def _antwort(payload, *, token):
        return {"ok": True, "text": json.dumps(
            {"verdict": "FIX", "next_action": "fix", "rationale": "x"}),
            "tokens": 1}

    chef._call = _antwort
    asyncio.run(chef.judge(context="l", allowed_builders=["codex"],
                           open_finding_ids=set(), criterion_keys=set(),
                           evidence_ids=set(), deterministic_keys=set()))
    require_equal(reihenfolge[:2], ["token", "lease"],
                  f"falsche Reihenfolge: {reihenfolge}")


def t_a_refused_lease_does_not_become_a_late_403() -> None:
    """Eine Kappe ist eine Kappe. Der Aufruf wird gar nicht erst gesendet."""
    gesendet: list[int] = []

    class _Broker:
        def register_principal(self, name):
            return "tok"

        def open_lease(self, principal, ref, *, deadline):
            raise RuntimeError("CapExceeded")

        def close_lease(self, lease_id):
            pass

    chef = LEAD.TechnicalLead(broker=_Broker(), port=1)

    async def _antwort(payload, *, token):
        gesendet.append(1)
        return {"ok": False, "reason": "broker_403"}

    chef._call = _antwort
    exc = require_raises(LEAD.LeadRefused, asyncio.run, chef.judge(
        context="l", allowed_builders=["codex"], open_finding_ids=set(),
        criterion_keys=set(), evidence_ids=set(), deterministic_keys=set()))
    require_equal(exc.detail, "lease_refused",
                  f"der Grund nennt nicht das Lease: {exc.detail}")
    require_equal(gesendet, [],
                  "der Aufruf ging trotz verweigertem Lease hinaus")


def t_an_invalid_verdict_is_a_finding_not_a_capacity_problem() -> None:
    """Ein Lead, der Unsinn liefert, ist kein fehlendes Kontingent.

    Der Unterschied zaehlt: `capacity` schickt den Milestone in eine
    Wartschleife und weckt ihn spaeter — bei einem ungueltigen Urteil waere
    das eine Schleife, die nie besser wird.
    """
    class _Unsinn:
        async def judge(self, **_k):
            raise LEAD.LeadRefused("unknown_verdict", "SHIP_IT")

    led, fahrer, _lead, _werk = _welt(adapters={"a": B.SyntheticBuilder()})
    fahrer.lead = _Unsinn()
    asyncio.run(fahrer.run("probe-a4", max_rounds=1))

    zustand = led.milestone("probe-a4")
    require(zustand.state != S.BLOCKED,
            "ein ungueltiges Urteil wurde als Kapazitaetsproblem behandelt")
    befunde = [f for f in led.findings("probe-a4") if f.origin == "lead"]
    require(befunde, "es entstand kein Finding ueber den Lead")
    require("unknown_verdict" in befunde[0].title,
            f"das Finding nennt den Grund nicht: {befunde[0].title}")


def t_an_unreachable_lead_is_a_capacity_problem() -> None:
    """Die Gegenprobe — ein nicht erreichbarer Lead IST eine Lage."""
    class _Weg:
        async def judge(self, **_k):
            raise LEAD.LeadRefused("lead_unreachable", "broker_403")

    led, fahrer, _lead, _werk = _welt(adapters={"a": B.SyntheticBuilder()})
    fahrer.lead = _Weg()
    zustand = asyncio.run(fahrer.run("probe-a4", max_rounds=1))
    require_equal(zustand, S.BLOCKED, f"falscher Zustand: {zustand}")
    require_equal(led.milestone("probe-a4").block_reason, "capacity",
                  "falscher Grund")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
