"""Selbst heilen, ohne sich selbst zu ermaechtigen.

Ein Arzt im eigenen System ist die gefaehrlichste Komponente, die bisher gebaut
wurde — gefaehrlicher als das Fachteam, gefaehrlicher als der Hintergrund. Denn
er hat einen Auftrag, der wie ein Freibrief klingt: „mach es wieder heil". Und er
handelt genau dann, wenn niemand hinsieht.

Drei Dinge werden hier deshalb in aller Schaerfe geprueft:

**Er kann sich nichts ausdenken.** Ein Vorgehen ist eine registrierte Funktion,
keine Zeichenkette. Es gibt im ganzen Modul keinen Ort, an dem aus Text ein
Befehl wuerde — und ein Test sucht danach im tokenisierten Quelltext.

**Er darf nicht behaupten, geholfen zu haben.** Ein Neustart, der ohne Fehler
durchlief, beweist nichts. Bewiesen ist es, wenn die Komponente danach wirklich
gesund antwortet, gemessen mit einer FRISCHEN Pruefung. Wer den
zwischengespeicherten Zustand von vorhin nimmt, meldet jeden Eingriff als Erfolg.

**Er darf nicht ewig versuchen.** Zweimal, dann Ruhe, dann ein Mensch. Ein
Wiederbelebungsversuch alle zwanzig Sekunden ist kein Selbstheilen.
"""
import asyncio
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import tokenize

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.control_center.health import HealthBoard, Probe, State  # noqa: E402
from solvio.doctor import playbooks as P  # noqa: E402
from solvio.doctor.diagnosis import (  # noqa: E402
    NEEDS_HUMAN, SELF_REPAIRABLE, Confidence, Diagnosis, RepairClass, conclude,
    healthy,
)
from solvio.doctor.doctor import (  # noqa: E402
    Doctor, Incident, MAX_ATTEMPTS, PERSIST_SECONDS, STABLE_SECONDS,
)
from solvio.doctor.store import DoctorStore  # noqa: E402

DOCTOR_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "solvio", "doctor")
SRC = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")


def _run(coro):
    return asyncio.run(coro)


def _code_only(path: str) -> str:
    """Quelltext ohne Kommentare und Zeichenketten — f-Strings eingeschlossen.

    Ab Python 3.12 sind f-String-Teile eigene Token; ein Filter, der nur STRING
    kennt, laesst ihren Inhalt durch. Genau daran ist derselbe Helfer im
    Kontrollzentrum einmal gescheitert.
    """
    skip = {tokenize.COMMENT, tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        value = getattr(tokenize, name, None)
        if value is not None:
            skip.add(value)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return " ".join(token.string for token in
                    tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type not in skip)


class _Dispatcher:
    def __init__(self, **kwargs):
        self.deep_service = kwargs.get("deep_service")
        self.deep_runtime = kwargs.get("deep_runtime")
        self.scheduler = kwargs.get("scheduler")
        self.browser = kwargs.get("browser")
        self.proactive_store = kwargs.get("proactive_store")
        self.capabilities = kwargs.get("capabilities")


def _board(component: str, states: list[State], *, reason: str = "kaputt",
           clock=None) -> HealthBoard:
    """Ein Brett, das bei jeder Pruefung den naechsten vorgegebenen Zustand liefert.

    Damit laesst sich „nach der Reparatur wieder gesund" ebenso herstellen wie
    „nach der Reparatur immer noch kaputt" — der Unterschied, um den es geht.
    """
    sequence = list(states)

    async def check():
        state = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return state, ("" if state is State.HEALTHY else reason)

    return HealthBoard([Probe(component, component.capitalize(), check, ttl=0.0)],
                       clock=clock)


def _doctor(component: str, states: list[State], *, dispatcher=None,
            reason: str = "kaputt", clock=None, store=None) -> Doctor:
    board = _board(component, states, reason=reason, clock=clock)
    return Doctor(dispatcher or _Dispatcher(), board, clock=clock, store=store)


def _fast(doctor: Doctor) -> None:
    """Die Beruhigungspause wegnehmen — sonst dauert jeder Test sechs Sekunden."""
    P.SETTLE_SECONDS = 0.0


def _restore_settle() -> None:
    P.SETTLE_SECONDS = 6.0


# -- Diagnose ist nicht Gesundheit -------------------------------------------

def t_a_healthy_component_yields_no_action():
    doctor = _doctor("hermes", [State.HEALTHY])
    found = _run(doctor.diagnose("hermes"))
    require_equal(found.repair, RepairClass.NO_ACTION, "nichts zu tun")
    require(not found.repair_available, "und kein Knopf")
    require_equal(found.confidence, Confidence.HIGH, "das weiss man sicher")


def t_a_cause_without_evidence_is_never_confident():
    """Eine Vermutung im Gewand einer Feststellung ist schlimmer als ein
    ehrliches Unbekannt."""
    with_two = conclude("hermes", State.UNAVAILABLE, cause="Prozess tot",
                        evidence=["returncode 1", "kein Socket"])
    with_one = conclude("hermes", State.UNAVAILABLE, cause="Prozess tot",
                        evidence=["returncode 1"])
    without = conclude("hermes", State.UNAVAILABLE, cause="Prozess tot")
    nothing = conclude("hermes", State.UNAVAILABLE)
    require_equal(with_two.confidence, Confidence.HIGH, "zwei Belege: hoch")
    require_equal(with_one.confidence, Confidence.MEDIUM, "einer: mittel")
    require_equal(without.confidence, Confidence.LOW, "keiner: niedrig")
    require_equal(nothing.confidence, Confidence.LOW, "gar nichts: niedrig")
    require_equal(nothing.as_dict()["ursache"], "nicht bekannt",
                  "und es wird auch so gesagt")


def t_an_expired_login_is_never_self_repairable():
    """Der Fall, in dem ein eifriger Arzt am meisten Schaden anrichtet."""
    doctor = _doctor("calendar", [State.AUTH_REQUIRED],
                     reason="Zugang abgelaufen — bitte neu anmelden",
                     clock=lambda: 1000.0)
    doctor._incidents["calendar"] = Incident("calendar", 0.0, 0.0)
    found = _run(doctor.diagnose("calendar"))
    require_equal(found.repair, RepairClass.REAUTH_REQUIRED, "das kann nur ein Mensch")
    require(not found.repair_available, "kein Selbstversuch")
    require(found.human_action_required, "und es wird als menschliche Sache gefuehrt")
    require("abgelaufen" in found.probable_cause, "die Ursache ist benannt")
    require(found.user_impact, "und was es fuer den Nutzer bedeutet")


def t_a_quota_limit_is_not_a_repair_case():
    doctor = _doctor("claude", [State.QUOTA_LIMITED], reason="Kontingent erschoepft",
                     clock=lambda: 1000.0)
    doctor._incidents["claude"] = Incident("claude", 0.0, 0.0)
    found = _run(doctor.diagnose("claude"))
    require_equal(found.repair, RepairClass.NO_ACTION, "das vergeht von selbst")
    require(not found.persistent, "und ist ausdruecklich nicht dauerhaft")


def t_a_brief_blip_is_not_yet_an_incident():
    """Ein Dienst, der zwei Sekunden nicht antwortet, braucht keinen Arzt."""
    now = [1000.0]
    doctor = _doctor("hermes", [State.UNAVAILABLE], dispatcher=_Dispatcher(),
                     clock=lambda: now[0])
    found = _run(doctor.diagnose("hermes"))
    require_equal(found.repair, RepairClass.NO_ACTION, "erst einmal abwarten")
    require(not found.persistent, "es gilt noch nicht als dauerhaft")

    now[0] += PERSIST_SECONDS + 1
    later = _run(doctor.diagnose("hermes"))
    require(later.persistent, "nach einer Minute schon")
    require_equal(later.repair, RepairClass.RESTART_COMPONENT, "und dann gibt es ein Vorgehen")
    require_equal(later.playbook, "hermes_restart", "naemlich dieses")


def t_a_component_without_a_playbook_says_so():
    now = [1000.0]
    doctor = _doctor("calendar", [State.UNAVAILABLE], reason="antwortet nicht",
                     clock=lambda: now[0])
    _run(doctor.diagnose("calendar"))
    now[0] += PERSIST_SECONDS + 1
    found = _run(doctor.diagnose("calendar"))
    require_equal(found.repair, RepairClass.UNSUPPORTED_REPAIR,
                  "dafuer gibt es kein zugelassenes Vorgehen")
    require(not found.repair_available, "und keinen Knopf")
    require_equal(found.playbook, "", "und kein erfundenes Vorgehen")


# -- Keine erfundenen Befehle -------------------------------------------------

def t_a_playbook_is_code_and_never_text():
    """Die wichtigste Bauform-Eigenschaft dieses Meilensteins."""
    for book in P.PLAYBOOKS.values():
        require(callable(book.action), f"{book.key}: die Handlung ist aufrufbar")
        require(inspect.iscoroutinefunction(book.action),
                f"{book.key}: und zwar eine Koroutine")
        require(book.action.__module__ == "solvio.doctor.playbooks",
                f"{book.key}: aus DIESER Datei, nicht uebergeben")
        require(not isinstance(book.action, str), f"{book.key}: kein Text")


def t_there_is_no_way_to_run_an_arbitrary_command():
    code = " ".join(_code_only(os.path.join(DOCTOR_DIR, name))
                    for name in ("playbooks.py", "doctor.py", "diagnosis.py",
                                 "store.py"))
    for forbidden in ("subprocess", "system", "popen", "shell", "eval",
                      "__import__", "compile"):
        require(forbidden not in code, f"'{forbidden}' im Arztpfad")
    # `exec(` mit Klammer: `executescript` und `execute` sind SQL und bleiben.
    require("exec (" not in code and "exec(" not in code,
            "kein exec-Aufruf")


def t_an_unknown_playbook_cannot_execute():
    async def scenario():
        doctor = _doctor("hermes", [State.UNAVAILABLE])
        made_up = conclude("hermes", State.UNAVAILABLE,
                           repair=RepairClass.RESTART_COMPONENT,
                           playbook="rm_minus_rf")
        return await doctor.repair(made_up)
    attempt = _run(scenario())
    require(not attempt.ran, "es wurde nichts ausgefuehrt")
    require(not attempt.recovered, "und nichts behauptet")
    require_equal(attempt.outcome, P.REPAIR_FAILED, "sondern abgelehnt")
    require("kein zugelassenes Vorgehen" in attempt.detail, "mit klarem Grund")


def t_a_playbook_cannot_be_applied_to_another_component():
    """Sonst koennte ein Befund ueber A eine Handlung an B ausloesen."""
    async def scenario():
        doctor = _doctor("browser", [State.UNAVAILABLE])
        crossed = conclude("browser", State.UNAVAILABLE,
                           repair=RepairClass.RESTART_COMPONENT,
                           playbook="hermes_restart")
        return await doctor.repair(crossed)
    attempt = _run(scenario())
    require(not attempt.ran, "nichts ausgefuehrt")
    require("gehoert nicht zu dieser Komponente" in attempt.detail, "und benannt")


def t_the_approval_gateway_and_the_core_have_no_playbook():
    """Ein Gateway-Neustart baut den Approver im Speicher neu auf und macht jede
    laufende Freigabe still gegenstandslos. Ein Core-Neustart erschiesst den
    Prozess, der die Entscheidungen haelt."""
    for component in ("gateway", "core", "approver"):
        require_equal(P.for_component(component), [],
                      f"{component} hat kein Vorgehen")
        require(component in P.FORBIDDEN_RESTARTS,
                f"{component} steht ausdruecklich auf der Sperrliste")
    for book in P.PLAYBOOKS.values():
        require(book.component not in P.FORBIDDEN_RESTARTS,
                f"{book.key} zielt auf eine gesperrte Komponente")


def t_the_playbook_set_is_small_and_named():
    require(len(P.PLAYBOOKS) <= 8, f"die Liste bleibt klein ({len(P.PLAYBOOKS)})")
    for key, book in P.PLAYBOOKS.items():
        require_equal(key, book.key, "der Schluessel ist die Kennung")
        require(book.verification, f"{key}: es gibt eine Nachpruefung")
        require(book.title, f"{key}: und einen Satz fuer den Menschen")


# -- Nachpruefung --------------------------------------------------------------

def t_a_repair_that_ran_but_did_not_help_is_not_a_success():
    """Der Fall, den ein naiver Arzt als Erfolg meldet."""
    _fast(_doctor("hermes", [State.UNAVAILABLE]))
    try:
        async def scenario():
            ran = {"n": 0}

            async def pretend(_dispatcher):
                ran["n"] += 1
                return True

            book = P.Playbook(key="tu_so", component="hermes", title="t",
                              repair=RepairClass.RESTART_COMPONENT,
                              action=pretend, verification="v")
            P.PLAYBOOKS["tu_so"] = book
            try:
                # Vor UND nach der Reparatur kaputt.
                doctor = _doctor("hermes", [State.UNAVAILABLE])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="tu_so")
                return await doctor.repair(found), ran["n"]
            finally:
                P.PLAYBOOKS.pop("tu_so", None)
        attempt, ran = _run(scenario())
    finally:
        _restore_settle()
    require_equal(ran, 1, "das Vorgehen lief")
    require(attempt.ran, "und meldete Erfolg")
    require(not attempt.recovered, "die Komponente wurde trotzdem nicht gesund")
    require_equal(attempt.outcome, P.INCONCLUSIVE,
                  "also ist der Ausgang unklar, nicht 'repariert'")
    require(P.RECOVERED != attempt.outcome, "und ausdruecklich nicht wiederhergestellt")


def t_a_repair_that_helped_is_reported_as_recovered():
    _fast(_doctor("hermes", [State.UNAVAILABLE]))
    try:
        async def scenario():
            async def works(_dispatcher):
                return True
            P.PLAYBOOKS["heilt"] = P.Playbook(
                key="heilt", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=works,
                verification="v")
            try:
                # `repair()` misst GENAU EINMAL, naemlich in der Nachpruefung.
                # Dort muss die Komponente gesund sein — das ist der Beweis.
                doctor = _doctor("hermes", [State.HEALTHY])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="heilt")
                return await doctor.repair(found), doctor
            finally:
                P.PLAYBOOKS.pop("heilt", None)
        attempt, doctor = _run(scenario())
    finally:
        _restore_settle()
    require(attempt.recovered, "die Komponente ist wieder gesund")
    require_equal(attempt.outcome, P.RECOVERED, "und das wird so gemeldet")
    incident = doctor._incidents.get("hermes")
    require(incident is not None and not incident.open,
            "der Vorfall gilt als behoben")


def t_verification_uses_a_fresh_probe_not_the_cache():
    """Das Gesundheitsbrett haelt Ergebnisse bis zu fuenf Minuten.

    Nach einer Reparatur ist genau dieser alte Wert die falsche Auskunft — er
    stammt aus der Zeit VOR dem Eingriff.
    """
    source = inspect.getsource(Doctor.verify)
    require("refresh([component])" in source or "refresh(\n" in source,
            "es wird ausdruecklich diese eine Komponente neu geprueft")
    require("board.refresh" in source, "und zwar ueber das Brett")

    _fast(_doctor("x", [State.HEALTHY]))
    try:
        async def scenario():
            calls = {"n": 0}

            async def counting():
                calls["n"] += 1
                return (State.UNAVAILABLE if calls["n"] == 1 else State.HEALTHY), ""
            # Eine LANGE Haltbarkeit: ohne einen erzwungenen frischen Blick
            # bliebe der erste (kaputte) Wert stehen.
            board = HealthBoard([Probe("x", "X", counting, ttl=9999.0)])
            doctor = Doctor(_Dispatcher(), board)
            await board.refresh()
            return await doctor.verify("x"), calls["n"]
        (recovered, _note), calls = _run(scenario())
    finally:
        _restore_settle()
    require(recovered, "die frische Pruefung sieht den neuen Zustand")
    require_equal(calls, 2, "es wurde wirklich noch einmal gefragt")


# -- Schleifenschutz -----------------------------------------------------------

def t_the_same_incident_is_attempted_at_most_twice():
    """Zweimal, dann Ruhe, dann ein Mensch."""
    _fast(_doctor("hermes", [State.UNAVAILABLE]))
    try:
        async def scenario():
            ran = {"n": 0}

            async def never_helps(_dispatcher):
                ran["n"] += 1
                return True
            P.PLAYBOOKS["zwecklos"] = P.Playbook(
                key="zwecklos", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=never_helps,
                verification="v", cooldown=0.0)
            try:
                doctor = _doctor("hermes", [State.UNAVAILABLE])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="zwecklos")
                outcomes = [await doctor.repair(found) for _ in range(5)]
                return ran["n"], outcomes
            finally:
                P.PLAYBOOKS.pop("zwecklos", None)
        ran, outcomes = _run(scenario())
    finally:
        _restore_settle()
    require_equal(ran, MAX_ATTEMPTS, f"hoechstens {MAX_ATTEMPTS} Versuche, waren {ran}")
    require(any("da sieht besser jemand nach" in o.detail for o in outcomes[2:]),
            "danach wird auf einen Menschen verwiesen")


def t_a_cooldown_holds_between_attempts():
    _fast(_doctor("hermes", [State.UNAVAILABLE]))
    try:
        async def scenario():
            ran = {"n": 0}

            async def useless(_dispatcher):
                ran["n"] += 1
                return True
            P.PLAYBOOKS["kuehl"] = P.Playbook(
                key="kuehl", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=useless,
                verification="v", cooldown=600.0)
            try:
                now = [1000.0]
                doctor = _doctor("hermes", [State.UNAVAILABLE],
                                 clock=lambda: now[0])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="kuehl")
                first = await doctor.repair(found)
                blocked = await doctor.repair(found)
                now[0] += 601
                after = await doctor.repair(found)
                return ran["n"], first, blocked, after
            finally:
                P.PLAYBOOKS.pop("kuehl", None)
        ran, first, blocked, after = _run(scenario())
    finally:
        _restore_settle()
    require(first.ran, "der erste Versuch laeuft")
    require(not blocked.ran, "der zweite sofort danach nicht")
    require("Ruhe" in blocked.detail, "und sagt, dass Ruhe gilt")
    require(after.ran, "nach der Abkuehlzeit wieder")
    require_equal(ran, 2, "insgesamt zwei Ausfuehrungen")


def t_a_user_tap_skips_the_cooldown_but_not_the_limit():
    """Ein ungeduldiger Finger darf keine Endlosschleife sein."""
    _fast(_doctor("hermes", [State.UNAVAILABLE]))
    try:
        async def scenario():
            ran = {"n": 0}

            async def useless(_dispatcher):
                ran["n"] += 1
                return True
            P.PLAYBOOKS["finger"] = P.Playbook(
                key="finger", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=useless,
                verification="v", cooldown=99999.0)
            try:
                doctor = _doctor("hermes", [State.UNAVAILABLE])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="finger")
                for _ in range(6):
                    await doctor.repair(found, requested_by_user=True)
                return ran["n"]
            finally:
                P.PLAYBOOKS.pop("finger", None)
        ran = _run(scenario())
    finally:
        _restore_settle()
    require_equal(ran, MAX_ATTEMPTS,
                  f"auch auf Zuruf hoechstens {MAX_ATTEMPTS}, waren {ran}")


def t_the_doctor_never_calls_itself():
    code = _code_only(os.path.join(DOCTOR_DIR, "doctor.py"))
    require("Doctor" not in code.replace("class Doctor", ""),
            "der Arzt erzeugt keinen zweiten Arzt")
    for name in ("heal", "repair", "diagnose"):
        body = inspect.getsource(getattr(Doctor, name))
        require("Doctor(" not in body, f"{name} baut keinen neuen Arzt")


# -- Autoritaet ---------------------------------------------------------------

def t_only_local_reversible_repairs_may_run_by_themselves():
    for book in P.PLAYBOOKS.values():
        require(book.repair in SELF_REPAIRABLE,
                f"{book.key}: {book.repair.value} laeuft nicht von selbst")
    for klass in NEEDS_HUMAN:
        require(not any(b.repair is klass for b in P.PLAYBOOKS.values()),
                f"es gibt kein Vorgehen fuer {klass.value}")


def t_no_playbook_touches_credentials_or_the_system():
    code = " ".join(_code_only(os.path.join(DOCTOR_DIR, name))
                    for name in ("playbooks.py", "doctor.py"))
    for forbidden in ("keychain", "credential", "token", "vault", "apt",
                      "brew", "sudo", "chmod", "rmtree", "unlink", "remove"):
        require(forbidden not in code.lower(), f"'{forbidden}' im Arztpfad")


def t_a_specialist_cannot_authorize_a_repair():
    """Der Rat eines Fachmanns ist Information, nie Befugnis."""
    code = " ".join(_code_only(os.path.join(DOCTOR_DIR, name))
                    for name in ("playbooks.py", "doctor.py", "diagnosis.py"))
    # `resolve` allein trifft die eigene Methode `_resolve`, die einen Vorfall
    # schliesst. Gesucht ist die ANBINDUNG an das Fachteam, nicht der Wortstamm.
    for forbidden in ("SpecialistTeam", "consult", "gap_resolver",
                      "GapResolver", "specialists"):
        require(forbidden not in code, f"'{forbidden}' im Arztpfad")


def t_untrusted_content_cannot_add_a_playbook():
    """Es gibt keinen Eingang dafuer — auch keinen gut gemeinten."""
    source = open(os.path.join(DOCTOR_DIR, "playbooks.py"), encoding="utf-8").read()
    for forbidden in ("def register", "def add_playbook", "PLAYBOOKS[", "update("):
        require(forbidden not in source,
                f"'{forbidden}': die Liste laesst sich von aussen erweitern")
    signature = inspect.signature(P.get)
    require_equal(list(signature.parameters), ["key"], "man kann nur nachschlagen")
    require(P.get("gibt_es_nicht") is None, "und Unbekanntes ergibt nichts")


# -- Vorfallgedaechtnis ---------------------------------------------------------

def t_the_incident_record_survives_and_holds_no_raw_logs():
    async def scenario():
        store = DoctorStore(os.path.join(tempfile.mkdtemp(), "d.sqlite3"))
        incident = Incident("hermes", 1000.0, 1100.0, attempts=2,
                            last_playbook="hermes_restart", last_result="unklar",
                            next_allowed=1700.0)
        await store.put_incident(incident)
        reopened = DoctorStore(store.path)
        return await reopened.open_incident("hermes"), await reopened.history()
    found, history = _run(scenario())
    require(found is not None, "der Vorfall ueberlebt")
    require_equal(found["attempts"], 2, "mit dem Zaehler")
    require_equal(found["last_playbook"], "hermes_restart", "und dem Vorgehen")
    require_equal(len(history), 1, "die Historie kennt ihn")
    blob = json.dumps(found)
    for forbidden in ("traceback", "Traceback", "/Users/", "stack"):
        require(forbidden not in blob, f"'{forbidden}' im Vorfall")


def t_the_doctor_store_closes_its_connections():
    """Dieselbe Lehre wie beim Hintergrundspeicher — hier von Anfang an."""
    raw = open(os.path.join(DOCTOR_DIR, "store.py"), encoding="utf-8").read()
    require("connection.close()" in raw, "es wird geschlossen")
    require("contextlib.contextmanager" in raw, "an einer Stelle")
    require("with sqlite3 . connect (" not in _code_only(
        os.path.join(DOCTOR_DIR, "store.py")),
            "der Transaktionskontext wird nicht fuer ein Schliessen gehalten")

    async def scenario():
        store = DoctorStore(os.path.join(tempfile.mkdtemp(), "d.sqlite3"))
        for index in range(60):
            await store.put_incident(Incident("x", 1000.0 + index, 1100.0))
            await store.history()
        return store.path
    path = _run(scenario())
    out = subprocess.run(["/usr/sbin/lsof", "-p", str(os.getpid())],
                         capture_output=True, text=True, timeout=30).stdout
    open_handles = sum(1 for line in out.splitlines() if path in line)
    require(open_handles <= 4, f"{open_handles} Verbindungen blieben offen")


def t_no_core_owned_store_mistakes_a_transaction_for_a_close():
    """Der Audit als Regressionstest — fuer die ganze Klasse.

    Der Fehler war einzigartig fuer den Hintergrundspeicher: alle anderen halten
    entweder eine dauerhafte Verbindung oder schliessen ausdruecklich. Damit das
    so bleibt, wird die Form hier festgehalten statt der Einzelfall.
    """
    stores = ("background/store.py", "conversation/store.py", "deep/journal.py",
              "memory/backup.py", "memory/privacy_ledger.py",
              "memory/semantic_index.py", "memory/sqlite_backend.py",
              "research/store.py", "security/mobile_approval/store.py",
              "proactive/store.py", "control_center/activity.py",
              "doctor/store.py")
    for relative in stores:
        path = os.path.join(SRC, relative)
        if not os.path.exists(path):
            continue
        # Tokenisiert: die Dateien ERKLAEREN das Muster in ihren Docstrings,
        # und eine Rohtext-Suche schlaegt genau dort an, wo es richtig gemacht
        # wird. Derselbe Fehler wie schon zweimal zuvor.
        code = _code_only(path)
        for pattern in ("with sqlite3 . connect (", "with self . _connect ( ) as",
                        "with connect ("):
            require(pattern not in code,
                    f"{relative}: ein Transaktionskontext wird fuer ein "
                    f"Schliessen gehalten")


def t_a_per_call_connection_is_closed_on_every_path_not_just_the_happy_one():
    """Die zweite Form desselben Fehlers: `close()` am Ende des `try`.

    `sqlite3.connect()` liest den Dateikopf nicht — eine beschaedigte Datei
    faellt erst beim ersten `PRAGMA` auf. Steht das Schliessen im `try` statt im
    `finally`, springt genau dieser Pfad daran vorbei. Der Verbindungs-Audit nach
    dem Leck im Hintergrundspeicher hat zwei solche Stellen in `memory/backup.py`
    gefunden; hier wird die Form festgehalten, nicht der Einzelfall.
    """
    import ast
    files = ("memory/backup.py", "proactive/store.py", "doctor/store.py",
             "control_center/activity.py", "research/store.py")
    for relative in files:
        path = os.path.join(SRC, relative)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if "sqlite3" not in body or "connect" not in body:
                continue
            # Eine Funktion, die selbst eine Verbindung oeffnet, muss sie auf
            # JEDEM Weg wieder schliessen: entweder ueber `finally`, ueber einen
            # Kontextmanager oder indem sie sie an ein Objekt uebergibt.
            has_finally = any(isinstance(child, ast.Try) and child.finalbody
                              for child in ast.walk(node))
            has_ctx = "contextmanager" in body or "closing" in body
            hands_over = "Attribute" in body and "self" in body
            require(has_finally or has_ctx or hands_over,
                    f"{relative}:{node.name} oeffnet eine Verbindung ohne "
                    f"finally, ohne Kontextmanager und ohne sie abzugeben")


def t_the_hermes_playbook_finds_the_service_where_the_core_puts_it():
    """Der Fehler aus dem ersten Live-Lauf.

    `deep_service` hing nur am CoreServer, nicht am Dispatcher — das Vorgehen
    griff ins Leere und meldete einen fehlgeschlagenen Neustart. Richtig
    gemeldet, aber vermeidbar. Der Test bindet beide Seiten aneinander, damit
    das Attribut nicht wieder auf einer Seite verschwindet.
    """
    body = inspect.getsource(P._restart_hermes)
    require('"deep_service"' in body, "das Vorgehen sucht deep_service")
    core = open(os.path.join(SRC, "realtime", "core_server.py"),
                encoding="utf-8").read()
    require("self.dispatcher.deep_service = deep" in core,
            "und der Core legt es dort auch ab")


def t_the_attempt_count_survives_a_restart_of_the_core():
    """Die Schleifenbremse muss einen Neustart ueberleben.

    Der schlimmste Fall ist genau der, den ein Test im Arbeitsspeicher nicht
    sieht: eine Komponente, die den Core mit hinunterreisst. Faengt die Zaehlung
    nach jedem Hochfahren bei null an, wird sie fuer immer neu gestartet — in
    ordentlichen Zweierschritten, die einzeln jedes Limit einhalten.

    Deshalb wird hier der Arzt weggeworfen und ein zweiter auf DERSELBEN Ablage
    gebaut. Er muss den alten Zaehler vorfinden.
    """
    _fast(None)
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "d.sqlite3")
            ran = {"n": 0}

            async def never_helps(_dispatcher):
                ran["n"] += 1
                return True

            P.PLAYBOOKS["zaeh"] = P.Playbook(
                key="zaeh", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=never_helps,
                verification="v", cooldown=0.0)
            try:
                async def scenario():
                    found = conclude("hermes", State.UNAVAILABLE,
                                     repair=RepairClass.RESTART_COMPONENT,
                                     playbook="zaeh")
                    first = _doctor("hermes", [State.UNAVAILABLE],
                                    store=DoctorStore(path))
                    for _ in range(MAX_ATTEMPTS):
                        await first.repair(found)
                    after_first = ran["n"]
                    # Der Core startet neu: neuer Arzt, neues Gedaechtnis,
                    # dieselbe Ablage.
                    second = _doctor("hermes", [State.UNAVAILABLE],
                                     store=DoctorStore(path))
                    refused = await second.repair(found)
                    return after_first, ran["n"], refused
                after_first, total, refused = _run(scenario())
            finally:
                P.PLAYBOOKS.pop("zaeh", None)
    finally:
        _restore_settle()
    require_equal(after_first, MAX_ATTEMPTS, "vor dem Neustart ausgereizt")
    require_equal(total, MAX_ATTEMPTS,
                  f"nach dem Neustart kein weiterer Versuch, waren {total}")
    require("da sieht besser jemand nach" in refused.detail,
            f"und der Grund wird genannt, war: {refused.detail!r}")


def t_the_background_round_closes_incidents_that_healed_themselves():
    """Ohne das erledigt sich im Hintergrund nie eine Akte.

    Die Runde befundet nur, was NICHT gesund ist — richtig so, sonst waere jede
    Minute eine volle Untersuchung. Aber damit sieht sie eine Komponente, die
    von selbst zurueckkam, nie wieder an. Ihre Akte bliebe offen, und der
    ausgereizte Zaehler wuerde die naechste Stoerung abweisen, die mit der
    alten nichts zu tun hat.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "d.sqlite3")

        async def scenario():
            store = DoctorStore(path)
            await store.put_incident(Incident(
                component="hermes", first_seen=time.time() - 120,
                last_seen=time.time() - 120, attempts=MAX_ATTEMPTS,
                last_playbook="hermes_restart", last_result="unklar"))
            doctor = _doctor("hermes", [State.HEALTHY], store=store)
            found = await doctor.diagnose_all()
            return found, await store.open_incident("hermes")
        found, still_open = _run(scenario())
    require_equal(found, [], "gesund heisst kein Befund")
    require(still_open is None,
            "aber die alte Akte wurde trotzdem abgehakt")


def t_a_component_that_recovers_by_itself_closes_its_incident():
    """Der Fall aus dem echten Lauf.

    Hermes kam nach einem Neustart des Core von selbst zurueck — ohne Zutun des
    Arztes. Ohne dauerhaftes Schliessen bleibt die Akte fuer immer offen, und
    der naechste Arzt findet eine offene Akte mit ausgereiztem Zaehler vor und
    verweigert die Behandlung einer laengst neuen Stoerung.

    Bewusst ueber `diagnose`, nicht ueber `repair`: nach einer Reparatur wird
    die Akte ohnehin am Ende festgehalten, dieser Weg hier ist der einzige, auf
    dem das Schliessen selbst schreiben muss.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "d.sqlite3")

        async def scenario():
            store = DoctorStore(path)
            stale = Incident(component="hermes", first_seen=time.time() - 120,
                             last_seen=time.time() - 120, attempts=MAX_ATTEMPTS,
                             last_playbook="hermes_restart",
                             last_result="reparatur_fehlgeschlagen")
            await store.put_incident(stale)
            # Ein frischer Arzt sieht eine gesunde Komponente.
            doctor = _doctor("hermes", [State.HEALTHY], store=store)
            found = await doctor.diagnose("hermes")
            return found, await store.open_incident("hermes")
        found, still_open = _run(scenario())
    require_equal(found.repair, RepairClass.NO_ACTION, "gesund, nichts zu tun")
    require(still_open is None,
            "und die alte Akte ist zu — sonst bleibt die Bremse fuer immer fest")


def t_restarting_hermes_rebinds_the_capabilities_to_the_new_process():
    """Der zweite Fehler aus dem Live-Lauf, und der gefaehrlichere.

    Beim ersten Anhaengen werden die Faehigkeiten im Router registriert und
    halten den Prozess in ihrer eigenen Referenz. Nach einem Neustart zeigt die
    Messung (`dispatcher.deep_runtime`) auf den neuen Prozess — die
    registrierten Handler aber weiter auf den toten. Das Kontrollzentrum meldete
    dann „in Ordnung", waehrend jede Recherche ins Leere lief: genau die zweite
    Wahrheit, die es nicht geben darf.

    Im echten Lauf schlug das als `ValueError` an — der Router wehrt sich zu
    Recht gegen eine doppelte Registrierung. Der Test prueft beides: es fliegt
    nicht mehr, UND die Faehigkeit folgt dem neuen Prozess.
    """
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.registry import attach_deep_runtime

    class _Runtime:
        def __init__(self, name):
            self.name = name

    class _Gate:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    dispatcher = _Dispatcher(capabilities=CapabilityRouter())
    dispatcher.capability_gate = _Gate()
    dispatcher.register = lambda tool: None

    first, second = _Runtime("alt"), _Runtime("neu")
    attach_deep_runtime(dispatcher, first)
    bound = dispatcher.deep_capabilities
    require(bound.runtime is first, "erst haengt der alte Prozess dran")

    attach_deep_runtime(dispatcher, second)   # darf nicht fliegen
    require(dispatcher.deep_runtime is second, "die Messung sieht den neuen")
    require(bound.runtime is second,
            "und die registrierte Faehigkeit auch — sonst zwei Wahrheiten")
    require(dispatcher.deep_capabilities is bound,
            "es bleibt dieselbe Instanz, die im Router steht")


def t_a_recovery_that_held_starts_a_fresh_count_but_flapping_does_not():
    """Die Bremse muss auch wieder loesen — aber nicht beim Flattern.

    Aufgefallen im Live-Lauf: nach einer geglueckten Reparatur stand der Zaehler
    auf 2. Zwei erfolgreiche Heilungen in sechs Stunden haetten die Komponente
    fuer den Rest des Fensters gesperrt. Das ist keine Schleifenbremse mehr,
    sondern Aufgeben.

    Der Unterschied ist nicht „schon mal versucht", sondern ob die Genesung
    GEHALTEN hat. Beide Richtungen stehen hier, weil nur eine davon zu pruefen
    die jeweils andere kaputtgehen laesst.
    """
    now = {"t": 1_000_000.0}
    doctor = _doctor("hermes", [State.HEALTHY], clock=lambda: now["t"])

    # Ein Vorfall, ausgereizt und dann behoben.
    incident = doctor._touch("hermes")
    incident.attempts = MAX_ATTEMPTS
    incident.resolved_at = now["t"]

    # a) Sofort wieder kaputt: derselbe Vorfall, die Bremse haelt.
    now["t"] += 30.0
    again = doctor._touch("hermes")
    require_equal(again.attempts, MAX_ATTEMPTS,
                  "Flattern setzt den Zaehler nicht zurueck")

    # b) Lange gesund geblieben, dann eine neue Stoerung: frischer Zaehler.
    again.resolved_at = now["t"]
    now["t"] += STABLE_SECONDS + 1
    fresh = doctor._touch("hermes")
    require_equal(fresh.attempts, 0,
                  f"nach {int(STABLE_SECONDS)}s Ruhe faengt es von vorn an, "
                  f"waren {fresh.attempts}")
    require(fresh.first_seen == now["t"], "und es ist wirklich ein neuer Vorfall")


def t_a_quiet_recovery_is_still_visible_in_the_activity_timeline():
    """Leise heisst nicht unsichtbar.

    Eine kurze Stoerung, die behoben wurde, erzeugt bewusst keine Meldung im
    Posteingang — sie hat niemanden gestoert. Nachvollziehbar muss sie trotzdem
    sein: „unsichtbar geheilt" ist von „ist nie passiert" nicht zu
    unterscheiden, und genau daraus entsteht das Misstrauen, das ein
    selbstheilendes System sich nicht leisten kann.
    """
    from solvio.control_center import activity as A

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "d.sqlite3")

        async def scenario():
            store = DoctorStore(path)
            now = time.time()
            await store.put_incident(Incident(
                component="hermes", first_seen=now - 120, last_seen=now - 60,
                attempts=1, last_playbook="hermes_restart",
                last_result="wiederhergestellt", resolved_at=now - 60))
            return await A.timeline(doctor_store=store, now=now)
        events = _run(scenario())

    require(events, "die Chronik kennt das Ereignis")
    first = events[0]
    require_equal(first.kind, "doctor", "es ist als Arztereignis erkennbar")
    require("Recherche" in first.title,
            f"und nennt die Komponente in Worten: {first.title!r}")
    require("wiederhergestellt" in first.title.lower(),
            f"und sagt, was passiert ist: {first.title!r}")
    require_equal(first.tone, "gut", "eine geglueckte Heilung ist nichts Rotes")


def t_healing_takes_no_target_from_the_model():
    """Das Modell darf ausloesen, nicht zielen.

    `system_heal` hat ein leeres Schema. Es kann sagen „richte, was kaputt ist"
    — was danach angefasst wird, entscheidet der Core aus seinem eigenen Befund
    gegen die geschlossene Liste der Vorgehen.

    Der zweite Teil erklaert, warum das nicht bloss Geschmack ist: mit einem
    modellgewaehlten Argument hebt der bestehende Vertrag das Risiko auf
    MUTATING, und dann verlangt derselbe Weg wie ueberall eine Freigabe. Es
    wurde also nichts gelockert, damit der Zuruf ohne Face ID auskommt — es
    wurde nur nichts hinzugefuegt, das eine noetig macht.
    """
    from solvio.capabilities.contract import (
        ArgumentSource, effective_risk, requires_approval)
    from solvio.capabilities.doctor import SPECS as C_SPECS
    from solvio.tools.doctor_capability_tools import _SCHEMAS

    heal = C_SPECS["system_heal"]
    require_equal(heal.input_schema.get("properties"), {},
                  "die Faehigkeit nimmt nichts entgegen")
    require_equal(_SCHEMAS["system_heal"]["parameters"].get("properties"), {},
                  "und das Modell sieht auch kein Feld")

    without = effective_risk(heal, {})
    require(not requires_approval(without),
            "ohne Ziel bleibt es so harmlos wie der Hintergrundarzt")
    with_target = effective_risk(heal, {"komponente": ArgumentSource.MODEL_DERIVED})
    require(requires_approval(with_target),
            "mit einem modellgewaehlten Ziel waere eine Freigabe faellig — "
            "die Bremse ist also noch scharf, sie wird nur nicht ausgeloest")


def t_asking_why_something_is_broken_names_the_human_boundary():
    """„Warum geht mein Kalender nicht?" bei abgelaufenem Zugang.

    Die Antwort darf nicht klingen, als koennte SOLVIO das gleich richten. Ein
    abgelaufener Zugang ist keine Stoerung, sondern etwas, das nur ein Mensch
    erneuern kann — und das gehoert in den Satz, nicht in eine Fussnote.
    """
    from solvio.capabilities.doctor import DoctorCapabilities

    doctor = _doctor("calendar", [State.AUTH_REQUIRED],
                     reason="401 invalid_grant")
    answer = _run(DoctorCapabilities(doctor).diagnose({"komponente": "kalender"}))
    spoken = answer["antwort"]
    require("Kalender" in spoken, f"nennt die Sache beim Namen: {spoken!r}")
    require("von Hand" in spoken or "nur jemand" in spoken,
            f"und sagt, dass es einen Menschen braucht: {spoken!r}")
    require("Mittel" not in spoken,
            f"und verspricht kein Vorgehen: {spoken!r}")


def t_healing_reports_what_it_could_not_do_instead_of_staying_silent():
    """„Repariere alles, was kaputt ist."

    Der stille Fehlschlag ist die gefaehrlichste Antwort: wer „erledigt" hoert
    und nichts nachprueft, glaubt an eine Heilung, die es nicht gab. Was einen
    Menschen braucht, wird deshalb genannt — nicht angefasst.
    """
    from solvio.capabilities.doctor import DoctorCapabilities

    probes = [Probe("calendar", "Kalender",
                    lambda: _fixed(State.AUTH_REQUIRED, "401 invalid_grant"),
                    ttl=0.0),
              Probe("hermes", "Recherche",
                    lambda: _fixed(State.HEALTHY, ""), ttl=0.0)]
    doctor = Doctor(_Dispatcher(), HealthBoard(probes))
    answer = _run(DoctorCapabilities(doctor).heal({}))
    require_equal(answer["behoben"], [], "es wurde nichts geheilt")
    require("calendar" in answer["braucht_dich"],
            f"der Kalender wird als deine Sache genannt: {answer!r}")
    require("braucht dich" in answer["antwort"],
            f"und zwar im Satz: {answer['antwort']!r}")


def t_having_no_remedy_is_not_reported_as_a_failed_attempt():
    """Aufgefallen im Live-Lauf.

    „Dein Zuhause habe ich nicht hinbekommen" klingt nach einem Versuch. Es gab
    keinen — fuer diese Komponente existiert gar kein Vorgehen. Der Unterschied
    ist nicht Wortklauberei: wer glaubt, SOLVIO habe es versucht, sucht den
    Fehler woanders.
    """
    from solvio.capabilities.doctor import DoctorCapabilities

    probes = [Probe("home_assistant", "Zuhause",
                    lambda: _fixed(State.UNAVAILABLE, "antwortet nicht"),
                    ttl=0.0)]
    doctor = Doctor(_Dispatcher(), HealthBoard(probes))
    answer = _run(DoctorCapabilities(doctor).heal({}))
    require_equal(answer["fehlgeschlagen"], [],
                  "nichts ist gescheitert, denn nichts wurde versucht")
    require("home_assistant" in answer["kein_mittel"],
            f"es steht als 'kein Mittel': {answer!r}")
    spoken = answer["antwort"]
    require("kein Mittel" in spoken, f"und sagt genau das: {spoken!r}")
    require("hinbekommen" not in spoken,
            f"und behauptet keinen Versuch: {spoken!r}")


async def _fixed(state, reason):
    return state, reason


def t_a_direct_question_does_not_wait_out_the_grace_period():
    """Wer fragt, hat es schon gemerkt.

    Die Schonfrist von einer Minute gibt es, damit der Hintergrund nicht auf
    jedes Zucken reagiert. Einem Menschen, der gerade fragt, „alles in Ordnung"
    zu antworten, weil die Stoerung erst zwanzig Sekunden alt ist, waere eine
    falsche Antwort mit gutem Gewissen.

    Die Obergrenze bleibt davon unberuehrt — das prueft
    `t_a_user_tap_skips_the_cooldown_but_not_the_limit`.
    """
    now = {"t": 5_000_000.0}
    doctor = _doctor("hermes", [State.UNAVAILABLE], clock=lambda: now["t"])

    quiet = _run(doctor.diagnose("hermes"))
    require_equal(quiet.repair, RepairClass.NO_ACTION,
                  "der Hintergrund wartet die Frist ab")
    require(not quiet.repair_available, "und bietet noch nichts an")

    asked = _run(doctor.diagnose("hermes", requested_by_user=True))
    require(asked.repair_available,
            "auf Nachfrage steht das Vorgehen sofort bereit")
    require_equal(asked.playbook, "hermes_restart", "und zwar das richtige")


def t_a_real_provider_auth_error_reaches_the_human_boundary():
    """Der Fund aus dem Live-Lauf mit einer echten Google-Antwort.

    Der Anbieter meldete `invalid_grant`. Die Kennung steht im `detail` des
    Ergebnisses — `reason` und `human_message` sind absichtlich allgemein. Weil
    die Messung nur diese beiden las, wurde aus „melde dich neu an" eine
    „Stoerung": der Nutzer bekam „ich weiss noch nicht, warum. Dafuer habe ich
    kein Mittel" statt des Anmeldedialogs.

    Die Zeichenketten hier sind die aus dem echten Lauf, nicht erfunden.
    """
    from solvio.control_center.probes import classify_error

    reason, detail, message = (
        "executor_unavailable",
        "ExecutorUnavailable: calendar authorization: invalid_grant",
        "Dafuer ist gerade nichts erreichbar — es ist nichts passiert.")

    without = classify_error(f"{reason} {message}")
    require(without[0] is not State.AUTH_REQUIRED,
            "ohne die Kennung ist es nicht zu erkennen — das war der Fehler")

    with_detail = classify_error(f"{reason} {detail} {message}")
    require_equal(with_detail[0], State.AUTH_REQUIRED,
                  "mit der Kennung trifft es zu")

    # Und die Messung muss sie auch wirklich mitgeben.
    source = open(os.path.join(SRC, "control_center", "probes.py"),
                  encoding="utf-8").read()
    require("result.detail" in source,
            "die Google-Pruefung reicht die Kennung an die Einstufung weiter")


def t_the_provider_detail_never_becomes_the_shown_reason():
    """Die Kennung darf einstufen, aber nicht auftreten.

    `detail` ist als internes Feld gebaut — es geht nie in den Modellkontext.
    Es zur Einstufung zu lesen ist richtig; es danach als Grund anzuzeigen waere
    ein Umweg, auf dem interne Diagnosetexte doch noch nach draussen kommen.
    """
    from solvio.control_center.probes import classify_error

    state, shown = classify_error(
        "executor_unavailable ExecutorUnavailable: calendar authorization: "
        "invalid_grant Dafuer ist gerade nichts erreichbar.")
    require_equal(state, State.AUTH_REQUIRED, "eingestuft wird es")
    require("ExecutorUnavailable" not in shown,
            f"aber der interne Text steht nicht im Grund: {shown!r}")
    require("invalid_grant" not in shown,
            f"und die Anbieterkennung auch nicht: {shown!r}")


def t_the_same_trouble_is_not_reported_twice_across_a_restart():
    """Der Fund aus dem echten Posteingang: dieselbe Meldung stand doppelt drin.

    Die Ruhezeit des Aufsehers lebt im Arbeitsspeicher und stirbt mit jedem
    Neustart des Core. Die Sperre in der Ablage haette das auffangen sollen —
    `UNIQUE(task_id, fingerprint)` — konnte es aber nie: SQLite haelt
    NULL-Werte in einer UNIQUE-Bedingung fuer VERSCHIEDEN, und `task_id` war
    bei Arztmeldungen immer NULL. Ein Schutz, der einen anderen verdeckt, bis
    beide fehlen.

    Der Test wirft den Aufseher weg und baut einen neuen auf derselben Ablage —
    genau das, was ein Neustart tut.
    """
    from solvio.doctor.supervisor import Supervisor
    from solvio.proactive.store import ProactiveStore

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "p.sqlite3")

        async def scenario():
            store = ProactiveStore(path)
            found = conclude("home_assistant", State.UNAVAILABLE,
                             symptoms=["antwortet nicht"],
                             cause="Die Komponente antwortet nicht",
                             repair=RepairClass.UNSUPPORTED_REPAIR,
                             persistent=True)

            # Zwei Zeitpunkte, die im ECHTEN Fall zu Duplikaten fuehrten: nur
            # elf Minuten auseinander, aber ueber eine Stundengrenze hinweg
            # (gemessen: 13:51 und 14:02, identischer Fingerabdruck). Der
            # frueher stuendliche Schluessel machte daraus zwei Meldungen.
            base = 21600.0 * 1000 + 3500.0
            first = Supervisor(None, store=store, clock=lambda: base)
            await first._tell(found, attempt=None, outage=600.0)
            # Der Core startet neu: neuer Aufseher, leeres Gedaechtnis.
            second = Supervisor(None, store=store, clock=lambda: base + 660.0)
            await second._tell(found, attempt=None, outage=600.0)
            return await store.unread(limit=50)
        items = _run(scenario())

    doctor_items = [i for i in items if i.get("quelle") == "doctor"]
    require_equal(len(doctor_items), 1,
                  f"genau eine Meldung, waren {len(doctor_items)}")


def t_an_expired_login_never_falls_back_to_paid_access():
    """Die teuerste Art, ein Anmeldeproblem zu „loesen".

    Ein abgelaufenes Abo laesst sich technisch umgehen: mit einem API-Schluessel,
    mit `--console`, mit einem Wechsel auf Abrechnung nach Verbrauch. Genau das
    darf nie passieren — es waere eine Rechnung, die niemand bestellt hat, und
    ein Weg an einer bewussten Entscheidung des Nutzers vorbei.

    Der Test prueft beides: dass in keinem Vorgehen so ein Ausweg steht, UND
    dass ein Befund mit abgelaufener Anmeldung ueberhaupt kein Vorgehen
    ausloest — er wird benannt, nicht behandelt.
    """
    verboten = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "--console",
                "api_key_fallback", "billing", "pay_as_you_go", "console.anthropic")
    for name in ("playbooks.py", "doctor.py", "supervisor.py", "diagnosis.py"):
        text = open(os.path.join(DOCTOR_DIR, name), encoding="utf-8").read()
        for wort in verboten:
            require(wort not in text,
                    f"{name} kennt keinen Ausweg auf Abrechnung ({wort})")

    # Und der Weg selbst: abgelaufene Anmeldung heisst kein Vorgehen.
    ran = {"n": 0}

    async def should_never_run(_dispatcher):
        ran["n"] += 1
        return True

    P.PLAYBOOKS["verlockung"] = P.Playbook(
        key="verlockung", component="claude", title="t",
        repair=RepairClass.RESTART_COMPONENT, action=should_never_run,
        verification="v", cooldown=0.0)
    try:
        doctor = _doctor("claude", [State.AUTH_REQUIRED],
                         reason="abgemeldet — bitte neu anmelden")
        diagnosis, attempt = _run(doctor.heal("claude", requested_by_user=True))
    finally:
        P.PLAYBOOKS.pop("verlockung", None)

    require_equal(diagnosis.repair, RepairClass.REAUTH_REQUIRED,
                  "es ist ein menschlicher Schritt")
    require(attempt is None, "und es wurde nichts versucht")
    require_equal(ran["n"], 0,
                  "insbesondere lief kein Vorgehen, obwohl eines eingetragen war")


# -- Der ganze Ablauf ------------------------------------------------------------

def t_heal_all_repairs_only_what_it_may_and_names_the_rest():
    """„Repariere alles" heisst nicht „tu alles, was noetig waere"."""
    _fast(_doctor("x", [State.HEALTHY]))
    try:
        async def scenario():
            done = {"n": 0}

            async def fixes(_dispatcher):
                done["n"] += 1
                return True
            P.PLAYBOOKS["heilbar"] = P.Playbook(
                key="heilbar", component="reparierbar", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=fixes,
                verification="v")
            try:
                async def broken():
                    return State.UNAVAILABLE, "kaputt"

                async def auth():
                    return State.AUTH_REQUIRED, "abgelaufen"

                async def fixed():
                    return State.HEALTHY, ""
                # Die Sonde bleibt kaputt. Geprueft wird nicht, ob es half,
                # sondern WAS ueberhaupt angefasst wurde — das ist die Frage
                # bei „repariere alles".
                now = [1000.0]
                board = HealthBoard([
                    Probe("reparierbar", "Reparierbar", broken, ttl=0.0),
                    Probe("calendar", "Kalender", auth, ttl=0.0),
                ], clock=lambda: now[0])
                doctor = Doctor(_Dispatcher(), board, clock=lambda: now[0])
                await doctor.diagnose_all()
                now[0] += PERSIST_SECONDS + 1
                results = await doctor.heal_all()
                return done["n"], results
            finally:
                P.PLAYBOOKS.pop("heilbar", None)
        done, results = _run(scenario())
    finally:
        _restore_settle()
    repaired = [d for d, a in results if a is not None]
    untouched = [d for d, a in results if a is None]
    require_equal(done, 1, "genau das Reparierbare wurde angefasst")
    require(all(not a.recovered for _d, a in results if a is not None),
            "und ehrlich gemeldet, dass es nicht half")
    require_equal([d.component for d in repaired], ["reparierbar"], "und nur das")
    require("calendar" in [d.component for d in untouched],
            "die Anmeldung bleibt liegen")
    require(all(d.human_action_required or not d.repair_available
                for d in untouched), "und wird als menschliche Sache gefuehrt")


def t_two_repairs_on_one_component_do_not_overlap():
    _fast(_doctor("x", [State.HEALTHY]))
    try:
        async def scenario():
            gate = asyncio.Event()
            ran = {"n": 0}

            async def slow(_dispatcher):
                ran["n"] += 1
                await gate.wait()
                return True
            P.PLAYBOOKS["langsam"] = P.Playbook(
                key="langsam", component="hermes", title="t",
                repair=RepairClass.RESTART_COMPONENT, action=slow,
                verification="v", cooldown=0.0)
            try:
                doctor = _doctor("hermes", [State.UNAVAILABLE])
                found = conclude("hermes", State.UNAVAILABLE,
                                 repair=RepairClass.RESTART_COMPONENT,
                                 playbook="langsam")
                first = asyncio.create_task(doctor.repair(found))
                await asyncio.sleep(0.1)
                second = await doctor.repair(found)
                gate.set()
                await first
                return ran["n"], second
            finally:
                P.PLAYBOOKS.pop("langsam", None)
        ran, second = _run(scenario())
    finally:
        _restore_settle()
    require_equal(ran, 1, "das Vorgehen lief nur einmal")
    require(not second.ran, "der zweite Anlauf wurde abgewiesen")
    require("laeuft bereits" in second.detail, "und sagt warum")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
