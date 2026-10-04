"""Autopilot A3 — Builder-Abstraktion, Checkpoint-Sicherheit, Capacity.

Drei Zusicherungen tragen diese Suite:

**Die Abstraktion haengt nicht an Codex.** Der Rest der Maschine kennt nur
`BuilderAdapter`. Ein gesperrter Adapter ist Teil der Registratur, nicht
weggelassen — sonst koennte spaeter niemand erklaeren, warum er fehlt.

**Ein Checkpoint committet nie blind** (Amendment 6). Findet der Scan
Anmeldematerial, wird nicht committet, nicht geerntet, nicht weitergereicht.
Und die Vormerkung wird zurueckgenommen, damit der naechste Schritt nicht
darauf aufsetzt.

**Rollen haben getrennte Kontingente** (Amendment 8). Ein Claude-Limit darf
keinen Codex-Build blockieren. `UNKNOWN` bleibt `UNKNOWN`.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a3-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.autopilot import builders as B   # noqa: E402
from solvio.autopilot import capacity as CAP  # noqa: E402
from solvio.autopilot import contract as C   # noqa: E402
from solvio.autopilot import store as S      # noqa: E402

BASIS = {"milestone_id": "probe-a3", "version": "1.0.0", "objective": "Z",
         "acceptance_criteria": [{"key": "a", "text": "t",
                                  "evidence_type": "DETERMINISTIC"}]}


def _welt():
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    led.create_milestone(C.parse(BASIS))
    return led, "probe-a3"


def _repo() -> str:
    pfad = tempfile.mkdtemp(prefix="klon-", dir=_SANDBOX)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
           "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"}
    subprocess.run(["git", "init", "--quiet", "-b", "haupt", pfad],
                   env=env, capture_output=True)
    with open(os.path.join(pfad, "LIESMICH"), "w") as fh:
        fh.write("start\n")
    with open(os.path.join(pfad, ".gitignore"), "w") as fh:
        fh.write("bauwerk/\n*.pyc\n")
    subprocess.run(["git", "-C", pfad, "add", "-A"], env=env, capture_output=True)
    subprocess.run(["git", "-C", pfad, "commit", "--quiet", "-m", "start"],
                   env=env, capture_output=True)
    return pfad


def _head(repo: str) -> str:
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------- die Abstraktion
def t_the_registry_never_hides_a_writer() -> None:
    """Ein weggelassener Adapter hinterlaesst keine Frage — ein gefuehrter
    hinterlaesst den Grund.

    **Was sich mit V0.6 geaendert hat, und was nicht.** Bis V0.5 war `claude`
    grundsaetzlich gesperrt, und diese Zusicherung hielt „genau ein realer
    Schreiber" fest. Seit V0.6 ist er der gemakelte Schreiber und damit
    grundsaetzlich erlaubt — ob er BAUEN kann, ist eine Kapazitaetsfrage
    (Anmeldung, Kontingent) und steht in `capacity.py`.

    Was unveraendert gilt und hier geprueft wird: die Registratur versteckt
    keinen Adapter, und wer gesperrt ist, traegt seinen Grund an der Sache.
    """
    ad = B.default_adapters()
    require("claude" in ad and "codex" in ad,
            f"ein Adapter fehlt in der Registratur: {sorted(ad)}")
    require_equal(B.writers(ad), ["claude", "codex"],
                  f"unerwartete Menge realer Schreiber: {B.writers(ad)}")

    # Und der alte Platzhalter bleibt abrufbar — mit seinem gemessenen Grund.
    alt = B.default_adapters(brokered_claude=False)
    require_equal(B.writers(alt), ["codex"],
                  f"der Rueckfall fuehrt mehr als einen Schreiber: "
                  f"{B.writers(alt)}")
    gesperrt = B.blocked_writers(alt)
    require_equal(sorted(gesperrt), ["claude"], "falsche Sperrliste")
    require("keychain" in gesperrt["claude"],
            f"der Sperrgrund nennt das Gate nicht: {gesperrt['claude']}")


def t_the_brokered_writer_is_blocked_by_a_red_canary_not_by_default() -> None:
    """Der Unterschied zwischen „darf nicht" und „kann gerade nicht".

    Beides fuehrte in V0.5 zum selben Ergebnis, weil es nur einen Grund gab.
    Jetzt gibt es zwei, und sie sind verschieden zu behandeln: eine Sperre ist
    endgueltig bis zu einer Codeaenderung, eine fehlende Anmeldung endet mit
    einer Owner-Handlung.
    """
    gesperrt = B.ClaudeWriterBuilder(canary_verdict="cli_canary_failed:x")
    require_equal(gesperrt.capability(), B.BLOCKED_BY_SECURITY_POLICY,
                  "ein roter Kanarienvogel sperrt nicht")

    frei = B.ClaudeWriterBuilder(canary_verdict="")
    require_equal(frei.capability(), B.WRITER,
                  "ein gruener Kanarienvogel sperrt trotzdem")

    # Die Anmeldung wird GESTELLT, nicht vom Rechner abgelesen: sobald der
    # Eigentuemer sie eingespielt hat, waere eine Zusicherung auf
    # `no_credential` schlicht falsch — und eine, die den Tresor braucht,
    # misst den Rechner statt die Regel.
    ohne = B.ClaudeWriterBuilder(canary_verdict="")
    ohne._credential_present = lambda: False
    require_equal(ohne.blocked_reason(), "no_credential",
                  f"ohne Anmeldung falscher Grund: {ohne.blocked_reason()}")

    mit = B.ClaudeWriterBuilder(canary_verdict="")
    mit._credential_present = lambda: True
    require_equal(mit.blocked_reason(), "",
                  f"mit Anmeldung trotzdem gesperrt: {mit.blocked_reason()}")

    # Und die Reihenfolge: die Sperre schlaegt die Lage.
    beides = B.ClaudeWriterBuilder(canary_verdict="cli_canary_failed:x")
    beides._credential_present = lambda: False
    require_equal(beides.blocked_reason(), "cli_canary_failed:x",
                  "die fehlende Anmeldung verdeckte die Sicherheitssperre")


def t_a_blocked_writer_refuses_instead_of_raising() -> None:
    """Eine Sperre ist eine Entscheidung, keine Stoerung.

    Wuerfe der Adapter, saehe der Failover eine Panne und wuerde
    wiederholen — bei einer Sicherheitsentscheidung ist Wiederholen genau
    falsch.
    """
    claude = B.ClaudeBuilder()
    ergebnis = asyncio.run(claude.build(B.BuildTask("m", "tu etwas"), "/tmp"))
    require_equal(ergebnis.outcome, B.REFUSED, "eine Sperre kam nicht als refused")
    require(not ergebnis.ok, "ein gesperrter Adapter meldete Erfolg")
    require(not ergebnis.quota, "eine Sperre wurde als Kontingent ausgegeben")
    require_equal(ergebnis.detail, B.ClaudeBuilder.REASON, "der Grund fehlt")


def t_every_adapter_answers_the_same_protocol() -> None:
    """Die Zusicherung hinter Amendment 3: ein spaeterer Writer aendert nur
    diese Datei. Deshalb muessen ALLE Adapter dasselbe koennen."""
    for adapter in (*B.default_adapters().values(), B.SyntheticBuilder()):
        for name in ("capability", "blocked_reason", "status", "build"):
            require(hasattr(adapter, name),
                    f"{adapter.name} kann kein {name}()")
        require(adapter.capability() in B.CAPABILITIES,
                f"{adapter.name} meldet eine unbekannte Faehigkeit")


# ------------------------------------------------------- Checkpoint-Sicherheit
def t_a_checkpoint_refuses_a_credential_and_takes_nothing_with_it() -> None:
    """Der Kern von Amendment 6.

    Geprueft wird nicht nur, dass nichts committet wird, sondern auch, dass die
    Vormerkung zurueckgenommen ist — sonst faende der naechste Schritt die
    Datei bereits im Index und truege sie weiter.
    """
    repo = _repo()
    vorher = _head(repo)
    with open(os.path.join(repo, "arbeit.py"), "w") as fh:
        fh.write("print('etwas Gutes')\n")
    with open(os.path.join(repo, "auth.json"), "w") as fh:
        fh.write('{"tokens": {"access_token": "geheim"}}\n')

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok, "ein Kredentialfund wurde committet")
    require_equal(ergebnis.reason, "credential_in_checkpoint", "falscher Grund")
    require(any("auth.json" in f for f in ergebnis.findings),
            f"die Datei wird nicht benannt: {ergebnis.findings}")
    require_equal(_head(repo), vorher, "es wurde trotzdem committet")

    vorgemerkt = subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "--name-only"],
        capture_output=True, text=True).stdout.strip()
    require_equal(vorgemerkt, "", f"die Vormerkung blieb stehen: {vorgemerkt}")


def t_a_checkpoint_also_catches_a_renamed_credential() -> None:
    """Ein umbenanntes `auth.json` heisst anders und ist dasselbe.

    Deshalb prueft der Scan Name UND Inhalt — der Name allein waere eine
    Pruefung, die man mit `mv` besiegt.
    """
    repo = _repo()
    with open(os.path.join(repo, "notizen.txt"), "w") as fh:
        fh.write('{"OPENAI_API_KEY": "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}\n')
    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok, "ein umbenanntes Geheimnis ging durch")
    require(any("content:" in f for f in ergebnis.findings),
            f"der Fund kam nicht aus dem Inhalt: {ergebnis.findings}")


def t_a_clean_checkpoint_commits_and_respects_gitignore() -> None:
    """Die Gegenprobe. Eine Pruefung, die immer verweigert, schuetzt nichts —
    die Agent Runtime hat das teuer gelernt (139 Treffer im ganzen Baum)."""
    repo = _repo()
    vorher = _head(repo)
    with open(os.path.join(repo, "arbeit.py"), "w") as fh:
        fh.write("def f():\n    return 1\n")
    os.makedirs(os.path.join(repo, "bauwerk"), exist_ok=True)
    with open(os.path.join(repo, "bauwerk", "muell.bin"), "w") as fh:
        fh.write("x" * 100)

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(ergebnis.ok, f"ein sauberer Checkpoint scheiterte: {ergebnis.reason}")
    require(ergebnis.commit and ergebnis.commit != vorher, "es wurde nicht committet")
    require_equal(sorted(ergebnis.changed), ["arbeit.py"],
                  f"falsche Dateimenge: {ergebnis.changed}")
    require(not any("bauwerk" in c for c in ergebnis.changed),
            ".gitignore wurde nicht respektiert")


def t_a_checkpoint_without_changes_is_not_an_error() -> None:
    """Nichts zu tun ist kein Fehlschlag — sonst waere jeder Resume einer."""
    repo = _repo()
    ergebnis = B.safe_checkpoint(repo, phase="resume")
    require(ergebnis.ok, "ein leerer Checkpoint galt als Fehler")
    require(ergebnis.nothing_to_do, "der Leerlauf wurde nicht als solcher gemeldet")
    require_equal(ergebnis.commit, _head(repo), "der Commit stimmt nicht")


def t_a_synthetic_builder_can_write_and_can_run_out_of_quota() -> None:
    """Das Werkzeug fuer den Failover-Beweis — beide Ausgaenge."""
    repo = _repo()
    schreiber = B.SyntheticBuilder(writes={"neu.txt": "Inhalt\n"})
    ergebnis = asyncio.run(schreiber.build(B.BuildTask("m", "schreib"), repo))
    require(ergebnis.ok, "der synthetische Builder schrieb nicht")
    require(os.path.isfile(os.path.join(repo, "neu.txt")), "die Datei fehlt")

    erschoepft = B.SyntheticBuilder(outcome=B.QUOTA)
    ergebnis = asyncio.run(erschoepft.build(B.BuildTask("m", "x"), repo))
    require(ergebnis.quota, "der gestellte Quota-Fall kam nicht an")
    require(not ergebnis.ok, "ein Quota-Ausgang galt als Erfolg")


# ------------------------------------------------------------------- Capacity
def t_capacity_states_follow_measurable_signals_in_order() -> None:
    """Erreichbarkeit vor Anmeldung vor Kontingent.

    Die Reihenfolge ist keine Kosmetik: ein nicht erreichbares Werkzeug ist
    nicht „erschoepft", und ein abgemeldetes ist kein Kontingentproblem. Wer
    sie vertauscht, nennt dem Menschen die falsche Handlung.
    """
    jetzt = 1_000_000.0
    faelle = (
        ("nicht erreichbar", dict(reachable=False, authenticated=False,
                                  last_success_at=0, last_quota_hit_at=0,
                                  known_reset_at=0), CAP.UNAVAILABLE),
        ("abgemeldet", dict(reachable=True, authenticated=False,
                            last_success_at=0, last_quota_hit_at=0,
                            known_reset_at=0), CAP.UNAVAILABLE),
        ("frischer Quota-Treffer", dict(reachable=True, authenticated=True,
                                        last_success_at=0,
                                        last_quota_hit_at=jetzt - 60,
                                        known_reset_at=0), CAP.EXHAUSTED),
        ("Reset in der Zukunft", dict(reachable=True, authenticated=True,
                                      last_success_at=0,
                                      last_quota_hit_at=jetzt - 99_999,
                                      known_reset_at=jetzt + 600), CAP.EXHAUSTED),
        ("alter Treffer, seither nichts", dict(reachable=True, authenticated=True,
                                               last_success_at=0,
                                               last_quota_hit_at=jetzt - 7_200,
                                               known_reset_at=0), CAP.LIMITED),
        ("alter Treffer, seither Erfolg", dict(reachable=True, authenticated=True,
                                               last_success_at=jetzt - 60,
                                               last_quota_hit_at=jetzt - 7_200,
                                               known_reset_at=0), CAP.AVAILABLE),
        ("alles gut", dict(reachable=True, authenticated=True,
                           last_success_at=jetzt - 10, last_quota_hit_at=0,
                           known_reset_at=0), CAP.AVAILABLE),
    )
    for name, signale, erwartet in faelle:
        bericht = CAP._from_signals(S.ROLE_BUILDER, "probe", now=jetzt, **signale)
        require_equal(bericht.state, erwartet, f"{name}: falscher Zustand")


def t_capacity_never_invents_a_remaining_token_count() -> None:
    """`UNKNOWN` bleibt `UNKNOWN`, und Signale bleiben `None` statt `0`.

    Eine Null saehe aus wie eine Messung. Sie waere eine Behauptung.
    """
    bericht = CAP._from_signals(S.ROLE_LEAD, "broker", reachable=True,
                                authenticated=True, last_success_at=0,
                                last_quota_hit_at=0, known_reset_at=0,
                                now=1.0)
    for schluessel in ("last_success_at", "last_quota_hit_at", "known_reset_at"):
        require(bericht.signals[schluessel] is None,
                f"{schluessel} wurde zu einer Zahl erfunden")
    for feld in bericht.signals.values():
        require(not isinstance(feld, float) or feld is None or feld > 0,
                "es steht eine erfundene Null in den Signalen")


def t_a_blocked_writer_is_unavailable_with_its_security_reason() -> None:
    """Damit der Bericht spaeter sagen kann, WARUM uebersprungen wurde."""
    led, mid = _welt()
    bericht = asyncio.run(CAP.builder_capacity(led, mid, B.ClaudeBuilder()))
    require_equal(bericht.state, CAP.UNAVAILABLE, "ein gesperrter Writer war nutzbar")
    require(bericht.signals.get("blocked_by_security_policy") is True,
            "der Sicherheitsgrund fehlt in den Signalen")
    require(not bericht.usable, "ein gesperrter Writer galt als nutzbar")


def t_one_roles_exhaustion_does_not_touch_another() -> None:
    """Amendment 8, als Verhalten statt als Absicht.

    Der Technical Lead haengt am Broker, der Builder an seinem CLI. Ein
    erschoepfter Builder darf den Review nicht anhalten — sonst waere der
    Autopilot bei jedem Kontingentende komplett stehen geblieben.
    """
    led, mid = _welt()
    jetzt = time.time()
    led.record_usage(mid, role=S.ROLE_BUILDER, provider="codex", note="quota",
                     now=jetzt - 60)
    led.record_usage(mid, role=S.ROLE_LEAD, provider="broker", note="ok",
                     now=jetzt - 30)

    builder = asyncio.run(CAP.builder_capacity(
        led, mid, B.SyntheticBuilder(), now=jetzt))
    require_equal(builder.state, CAP.EXHAUSTED,
                  "der Quota-Treffer des Builders wurde nicht gesehen")

    lead = asyncio.run(CAP.lead_capacity(led, mid, now=jetzt))
    require(lead.state != CAP.EXHAUSTED,
            "das Builder-Kontingent hat den Technical Lead mitgerissen")


def t_choosing_a_builder_prefers_available_and_is_deterministic() -> None:
    """Ein Failover, der wuerfelt, ist nicht nachvollziehbar."""
    jetzt = 1_000.0
    lage = {
        "codex": CAP.CapacityReport(S.ROLE_BUILDER, "codex", CAP.EXHAUSTED, {}, jetzt),
        "synthetic": CAP.CapacityReport(S.ROLE_BUILDER, "synthetic", CAP.AVAILABLE, {}, jetzt),
        "claude": CAP.CapacityReport(S.ROLE_BUILDER, "claude", CAP.UNAVAILABLE, {}, jetzt),
    }
    require_equal(CAP.choose_builder(lage), "synthetic", "falsche Wahl")
    require_equal(CAP.choose_builder(lage), "synthetic", "zweimal anders gewaehlt")
    require_equal(CAP.choose_builder(lage, exclude=("synthetic",)), "",
                  "ein erschoepfter oder gesperrter Builder wurde gewaehlt")

    lage["codex"] = CAP.CapacityReport(S.ROLE_BUILDER, "codex", CAP.LIMITED, {}, jetzt)
    require_equal(CAP.choose_builder(lage), "synthetic",
                  "LIMITED wurde AVAILABLE vorgezogen")
    require_equal(CAP.choose_builder(lage, exclude=("synthetic",)), "codex",
                  "der LIMITED-Builder wurde nicht als Rueckfall genommen")


def t_a_limited_builder_only_takes_small_tasks() -> None:
    """Eine grosse Aufgabe im knappen Kontingent endet im Failover — und dann
    war die halbe Arbeit umsonst."""
    knapp = CAP.CapacityReport(S.ROLE_BUILDER, "codex", CAP.LIMITED, {}, 1.0)
    require(knapp.accepts("SMALL"), "eine kleine Aufgabe wurde abgelehnt")
    require(not knapp.accepts("LARGE"), "eine grosse Aufgabe wurde angenommen")
    frei = CAP.CapacityReport(S.ROLE_BUILDER, "codex", CAP.AVAILABLE, {}, 1.0)
    require(frei.accepts("LARGE"), "ein freier Builder lehnte gross ab")


# ------------------------------------------------------- MODEL_FIT / ROUTE
def t_model_fit_is_decided_without_knowing_what_is_available() -> None:
    """Die Kernregel: Sicherheit, Quota und Kosten duerfen MODEL_FIT nicht
    nachtraeglich verfaelschen.

    Geprueft am Verhalten: derselbe Auftrag, zweimal, einmal mit gesperrtem
    und einmal mit freiem Claude. MODEL_FIT muss BEIDE Male gleich sein — nur
    ROUTE und der Grund duerfen sich unterscheiden.
    """
    from solvio.autopilot import routing as RT

    frei = lambda n, z: CAP.CapacityReport(S.ROLE_BUILDER, n, z, {}, 1.0)

    # Ein wirklich GESPERRTER Claude — seit V0.6 ist das der mit rotem
    # Kanarienvogel, nicht mehr der Vorgabeadapter.
    gesperrt = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                         adapters={"codex": B.CodexBuilder(),
                                   "claude": B.ClaudeWriterBuilder(
                                       canary_verdict="cli_canary_failed:x")},
                         options={"codex": frei("codex", CAP.AVAILABLE),
                                  "claude": frei("claude", CAP.UNAVAILABLE)})

    class _FreierClaude(B.SyntheticBuilder):
        name = "claude"

    offen = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                      adapters={"codex": B.CodexBuilder(),
                                "claude": _FreierClaude()},
                      options={"codex": frei("codex", CAP.AVAILABLE),
                               "claude": frei("claude", CAP.AVAILABLE)})

    require_equal(gesperrt.model_fit, offen.model_fit,
                  "MODEL_FIT haengt an der Verfuegbarkeit — genau das darf es nicht")
    require_equal(gesperrt.model_fit, "claude",
                  f"die Qualitaetswahl stimmt nicht: {gesperrt.model_fit}")
    require_equal(gesperrt.route, "codex", "die Route stimmt nicht")
    require_equal(gesperrt.reason, RT.SECURITY_POLICY,
                  f"falscher Grund: {gesperrt.reason}")
    require(gesperrt.compromised, "die Abweichung wird nicht als solche gefuehrt")
    require(not offen.compromised, "eine Route ohne Abweichung galt als kompromittiert")

    # Und die dritte Lage, die es seit V0.6 gibt: erlaubt, aber ohne
    # Anmeldung. MODEL_FIT bleibt derselbe, der GRUND ist ein anderer — und
    # das ist der Unterschied zwischen „da hilft eine Handlung" und „da hilft
    # Warten".
    ohne = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                     adapters={"codex": B.CodexBuilder(),
                               "claude": B.ClaudeWriterBuilder(
                                   canary_verdict="")},
                     options={"codex": frei("codex", CAP.AVAILABLE),
                              "claude": frei("claude", CAP.UNAVAILABLE)})
    require_equal(ohne.model_fit, "claude",
                  "MODEL_FIT verschob sich mit der Anmeldung")
    require_equal(ohne.reason, RT.UNAVAILABLE,
                  f"fehlende Anmeldung als {ohne.reason} gemeldet")


def t_the_route_is_obeyed_not_only_booked() -> None:
    """Eine gebuchte Route, der niemand folgt, ist schlimmer als keine.

    Live gemessen in der V0.6-Abnahme: die Route sagte `actual = codex`, und
    gebaut hat Claude — die Wahl hing an `choose_builder`, das alphabetisch
    sortiert. In V0.5 fiel das nie auf, weil es genau einen Schreiber gab und
    beide Wege zum selben Namen kamen.

    Geprueft am Verhalten: bei einer Reparaturaufgabe bevorzugt MODEL_FIT
    Codex — und dann muss Codex bauen, obwohl `claude` alphabetisch vorn
    stuende.
    """
    from solvio.autopilot import routing as RT

    gebaut: list[str] = []

    class _Merker(B.SyntheticBuilder):
        def __init__(self, name):
            super().__init__(writes={"x.txt": "1\n"})
            self.name = name

        async def build(self, task, workdir):
            gebaut.append(self.name)
            return await super().build(task, workdir)

    adapter = {"claude": _Merker("claude"), "codex": _Merker("codex")}
    frei = lambda n: CAP.CapacityReport(S.ROLE_BUILDER, n, CAP.AVAILABLE, {}, 1.0)
    optionen = {"claude": frei("claude"), "codex": frei("codex")}

    reparatur = RT.decide(trigger=RT.NEW_TASK, task_kind="repair",
                          adapters=adapter, options=optionen, size=B.MEDIUM)
    require_equal(reparatur.route, "codex",
                  f"die Route fuer eine Reparatur ist nicht codex: "
                  f"{reparatur.route}")

    entwurf = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                        adapters=adapter, options=optionen, size=B.MEDIUM)
    require_equal(entwurf.route, "claude",
                  f"die Route fuer einen Entwurf ist nicht claude: "
                  f"{entwurf.route}")

    # Und die Wahl des Treibers muss der Route folgen — nicht dem Alphabet.
    require(entwurf.route != reparatur.route,
            "die beiden Routen unterscheiden sich nicht; der Fall ist "
            "ungestellt")
    from solvio.autopilot import driver as DRV
    quelle = __import__("inspect").getsource(DRV.Driver._build_phase)
    require("route.route" in quelle,
            "der Treiber liest die Route nicht, wenn er den Builder waehlt")
    require(quelle.index("route.route") < quelle.index("CAP.choose_builder"),
            "der Treiber fragt das Alphabet vor der Route")


def t_the_route_line_names_preferred_actual_and_reason() -> None:
    """Genau die Form, die der Auftrag verlangt."""
    from solvio.autopilot import routing as RT
    frei = lambda n, z: CAP.CapacityReport(S.ROLE_BUILDER, n, z, {}, 1.0)
    route = RT.decide(trigger=RT.BUILDER_FAILOVER, task_kind="repair",
                      adapters=B.default_adapters(),
                      options={"codex": frei("codex", CAP.AVAILABLE),
                               "claude": frei("claude", CAP.UNAVAILABLE)})
    zeile = route.line()
    for teil in ("preferred =", "actual =", "reason ="):
        require(teil in zeile, f"{teil} fehlt in der Zeile: {zeile}")


def t_capacity_shortage_is_named_as_capacity_not_as_preference() -> None:
    """Ein knappes Kontingent ist kein Qualitaetsurteil.

    Wuerde es als `best_fit` gebucht, saehe eine dauerhafte Notloesung nach
    drei Monaten aus wie eine Architekturentscheidung.
    """
    from solvio.autopilot import routing as RT

    class _Claude(B.SyntheticBuilder):
        name = "claude"

    frei = lambda n, z: CAP.CapacityReport(S.ROLE_BUILDER, n, z, {}, 1.0)
    route = RT.decide(trigger=RT.REPEATED_FAILURE, task_kind="implementation",
                      adapters={"claude": _Claude(), "codex": B.CodexBuilder()},
                      options={"claude": frei("claude", CAP.EXHAUSTED),
                               "codex": frei("codex", CAP.AVAILABLE)})
    require_equal(route.model_fit, "claude", "die Qualitaetswahl stimmt nicht")
    require_equal(route.route, "codex", "die Route stimmt nicht")
    require_equal(route.reason, RT.CAPACITY,
                  f"ein Kontingentmangel wurde als {route.reason} gebucht")
    require("EXHAUSTED" in route.detail, f"der Zustand fehlt: {route.detail}")


def t_a_route_is_only_reconsidered_at_named_triggers() -> None:
    """Keine Neuentscheidung bei jedem Minischritt."""
    from solvio.autopilot import routing as RT
    require_equal(sorted(RT.TRIGGERS),
                  ["builder_failover", "new_finding", "new_phase", "new_task",
                   "repeated_failure"],
                  f"unerwartete Anlassliste: {sorted(RT.TRIGGERS)}")
    require_raises(ValueError, RT.decide, trigger="weil_ich_lust_habe",
                   task_kind="implementation", adapters={}, options={})


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
