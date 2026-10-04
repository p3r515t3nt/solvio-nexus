"""Autopilot A2 — Evidence und Test-Gate.

Zwei Haltungen werden hier gestellt:

**Fail closed.** Eine Gate-Ausgabe ohne lesbare Zusammenfassung ist NICHT
gruen. Das ist die `_do_verify`-Lehre der Agent Runtime in anderer Gestalt:
was nicht nachweislich in Ordnung ist, laesst nichts gelingen. Eine Suite, die
nur auf `failed>0` prueft, laesst genau die Faelle durch, die niemand erwartet
hat — abgebrochene Laeufe, Basislinien-Abweichung, `passed != executed`.

**Die Umgebung gehoert zur Messung.** Beim Offsite-Release fielen 14
Zusicherungen in einer Umgebung ohne Extras. Ein Testbericht ohne
Umgebungsangabe beweist nichts ueber den Code.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-a2-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.autopilot import contract as C   # noqa: E402
from solvio.autopilot import evidence as E   # noqa: E402
from solvio.autopilot import store as S      # noqa: E402

GRUEN = ("Suites: 108   EXPECTED=2950 EXECUTED=2944 PASSED=2944 FAILED=0"
         " SKIPPED=6\nMissing=0 BaselineDrift=0")

BASIS = {"milestone_id": "probe-a2", "version": "1.0.0", "objective": "Z",
         "acceptance_criteria": [{"key": "gate", "text": "Gate",
                                  "evidence_type": "DETERMINISTIC"}]}


def _welt():
    pfad = tempfile.mkdtemp(dir=_SANDBOX) + "/ap.sqlite3"
    led = S.AutopilotLedger(pfad)
    led.create_milestone(C.parse(BASIS))
    return led, "probe-a2"


def _repo(name: str = "arbeit") -> str:
    """Ein echtes kleines git-Repository. Keine Attrappe: die Funktionen hier
    reden mit git, und eine Attrappe wuerde nur meine Erwartung pruefen."""
    pfad = tempfile.mkdtemp(prefix=name + "-", dir=_SANDBOX)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
           "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"}
    for args in (["init", "--quiet", "-b", "haupt", pfad],
                 ["-C", pfad, "add", "-A"]):
        subprocess.run(["git", *args], env=env, capture_output=True, check=False)
    with open(os.path.join(pfad, "LIESMICH"), "w") as fh:
        fh.write("erste Fassung\n")
    subprocess.run(["git", "-C", pfad, "add", "-A"], env=env, capture_output=True)
    subprocess.run(["git", "-C", pfad, "commit", "--quiet", "-m", "erster"],
                   env=env, capture_output=True)
    return pfad


def _commit(repo: str, datei: str, inhalt: str, nachricht: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
           "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"}
    with open(os.path.join(repo, datei), "w") as fh:
        fh.write(inhalt)
    subprocess.run(["git", "-C", repo, "add", "-A"], env=env, capture_output=True)
    subprocess.run(["git", "-C", repo, "commit", "--quiet", "-m", nachricht],
                   env=env, capture_output=True)
    return E.head_commit(repo)


# -------------------------------------------------------------- fail closed
def t_an_unreadable_gate_output_is_never_green() -> None:
    """Der wichtigste Fall, weil er der unerwartete ist.

    Ein Gate, dessen Zusammenfassung fehlt, hat entweder nicht zu Ende
    gelaufen oder etwas anderes getan. Exit-Code 0 aendert daran nichts.
    """
    for text, code in (("", 0), ("Traceback ...", 0), ("laeuft noch", 0),
                       ("Suites: 3", 0)):
        r = E.parse_gate(text, code)
        require(not r.ok, f"unlesbare Ausgabe galt als gruen: {text!r}")
        require_equal(r.reason, "unreadable_summary", "falscher Grund")
        require(not r.parsed, "eine unlesbare Ausgabe galt als geparst")


def t_a_green_exit_code_alone_does_not_make_a_green_gate() -> None:
    """Vier Wege, auf denen ein Gate rot ist, ohne dass der Exit-Code es sagt.

    Die Basislinien-Abweichung ist der praktisch wichtigste: sie hat beim
    Offsite-Release wirklich einen Lauf rot gemacht, den die Zahlenzeile
    ansonsten gruen zeigte.
    """
    faelle = (
        ("rote Tests", GRUEN.replace("PASSED=2944 FAILED=0", "PASSED=2930 FAILED=14"), "failed=14"),
        ("Basislinien-Abweichung", GRUEN.replace("BaselineDrift=0", "BaselineDrift=17"), "baseline_drift=17"),
        ("fehlende Tests", GRUEN.replace("Missing=0", "Missing=3"), "missing=3"),
        ("bestanden != ausgefuehrt",
         GRUEN.replace("EXECUTED=2944 PASSED=2944", "EXECUTED=2944 PASSED=2940"),
         "passed!=executed"),
    )
    for name, text, marker in faelle:
        r = E.parse_gate(text, 0)          # Exit-Code sagt „alles gut"
        require(not r.ok, f"{name}: galt trotz Exit 0 als gruen")
        require(marker in r.reason, f"{name}: der Grund nennt es nicht ({r.reason})")


def t_a_truly_green_gate_is_recognised() -> None:
    """Die Gegenprobe. Eine Pruefung, die nie gruen sagt, sichert nichts."""
    r = E.parse_gate(GRUEN, 0)
    require(r.ok, f"ein gruenes Gate galt als rot: {r.reason}")
    require_equal((r.passed, r.failed, r.suites), (2944, 0, 108), "falsch gelesen")
    require_equal(r.reason, "", "ein gruenes Gate nannte einen Grund")


def t_a_timeout_is_not_a_pass() -> None:
    """Ein Gate, das nicht zurueckkommt, hat nichts bewiesen."""
    repo = _repo()
    with open(os.path.join(repo, "scripts_stub"), "w") as fh:
        fh.write("")
    os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
    with open(os.path.join(repo, E.GATE_SCRIPT), "w") as fh:
        fh.write("import time\ntime.sleep(30)\n")
    venv = os.path.join(repo, ".venv", "bin")
    os.makedirs(venv, exist_ok=True)
    os.symlink(sys.executable, os.path.join(venv, "python"))
    r = E.run_gate(repo, timeout=1.0)
    require(not r.ok, "ein Timeout galt als Erfolg")
    require("timeout" in r.reason, f"der Grund nennt kein Timeout: {r.reason}")


def t_a_missing_gate_or_interpreter_is_an_error_not_a_pass() -> None:
    repo = _repo()
    exc = require_raises(E.EvidenceError, E.run_gate, repo)
    require_equal(exc.reason, "gate_missing", "falscher Grund")
    os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
    with open(os.path.join(repo, E.GATE_SCRIPT), "w") as fh:
        fh.write("print('x')\n")
    exc = require_raises(E.EvidenceError, E.run_gate, repo)
    require_equal(exc.reason, "interpreter_missing", "falscher Grund")


# ------------------------------------------------------------- die Umgebung
def t_the_fingerprint_notices_a_missing_module() -> None:
    """Genau der Unterschied, der beim Offsite-Release 14 Zusicherungen
    umwarf — er muss sich im Fingerabdruck niederschlagen."""
    repo = _repo()
    echt = E.environment_fingerprint(repo)
    original = E.importlib.util.find_spec

    def ohne_yaml(name, *a, **k):
        if name == "yaml":
            return None
        return original(name, *a, **k)

    E.importlib.util.find_spec = ohne_yaml
    try:
        ohne = E.environment_fingerprint(repo)
        detail = E.environment_detail(repo)
    finally:
        E.importlib.util.find_spec = original

    require(echt != ohne, "ein fehlendes Modul aenderte den Fingerabdruck nicht")
    require("yaml" in detail["module_fehlend"], "das fehlende Modul wird nicht benannt")
    require("all-extras" in detail["hinweis"],
            "der Hinweis nennt nicht den kanonischen Einrichtungsweg")


def t_the_fingerprint_notices_a_changed_lockfile() -> None:
    repo = _repo()
    with open(os.path.join(repo, "uv.lock"), "w") as fh:
        fh.write("version = 1\n")
    eins = E.environment_fingerprint(repo)
    with open(os.path.join(repo, "uv.lock"), "w") as fh:
        fh.write("version = 2\n")
    zwei = E.environment_fingerprint(repo)
    require(eins != zwei, "eine geaenderte Sperrdatei aenderte nichts")


# ------------------------------------------------------- Buchung und Frische
def t_gate_evidence_is_reused_only_for_the_same_commit_and_environment() -> None:
    """Wiederverwendung spart Zeit — aber nur, wo sie ehrlich ist.

    Und sie wird gebucht: eine Kostenrechnung, die wiederverwendete Messungen
    verschweigt, sieht sparsamer aus, als der Lauf war.
    """
    led, mid = _welt()
    repo = _repo()
    stand = E.head_commit(repo)
    fingerabdruck = E.environment_fingerprint(repo)

    erste = led.record_evidence(mid, kind="test_report", commit=stand,
                                env_fingerprint=fingerabdruck, ok=True,
                                summary="gruen", payload={"passed": 10,
                                                          "executed": 10})
    eid, ergebnis, wieder = E.record_gate(led, mid, repo, commit=stand)
    require(wieder, "eine gueltige Messung wurde nicht wiederverwendet")
    require_equal(eid, erste, "es kam eine andere Evidence zurueck")
    require(ergebnis.ok, "die wiederverwendete Messung verlor ihr Urteil")
    arten = [e["kind"] for e in led.events(mid)]
    require("evidence_reused" in arten,
            "die Wiederverwendung wurde nicht gebucht")

    # Anderer Commit: es gibt nichts zu erben.
    neuer = _commit(repo, "LIESMICH", "zweite Fassung\n", "zweiter")
    require(led.fresh_evidence(mid, kind="test_report", commit=neuer,
                               env_fingerprint=fingerabdruck) is None,
            "Evidence eines anderen Commits galt als frisch")


def t_a_commit_evidence_carries_the_changed_files() -> None:
    """Was der Builder wirklich angefasst hat — aus git, nicht aus seinem Wort."""
    led, mid = _welt()
    repo = _repo()
    basis = E.head_commit(repo)
    _commit(repo, "neu.txt", "Inhalt\n", "eine Datei dazu")

    eid = E.record_commit(led, mid, repo, base=basis)
    beleg = led.evidence(eid)
    require(beleg is not None, "die Evidence wurde nicht gebucht")
    import json
    nutzlast = json.loads(beleg.payload_json)
    require_equal(sorted(nutzlast["changed_files"]), ["neu.txt"],
                  f"die geaenderten Dateien stimmen nicht: {nutzlast['changed_files']}")
    require(nutzlast["diffstat"], "der Diffstat fehlt")
    require_equal(beleg.commit, E.head_commit(repo), "der Commit stimmt nicht")


def t_the_diff_is_capped_and_says_so() -> None:
    """Ein Deckel, der schweigt, ist eine Luege ueber die Vollstaendigkeit."""
    repo = _repo()
    basis = E.head_commit(repo)
    _commit(repo, "gross.txt", "\n".join(f"zeile {i}" for i in range(5_000)),
            "viel Text")
    kurz = E.diff_text(repo, basis, max_chars=500)
    require(len(kurz) < 2_000, "der Deckel hat nicht gegriffen")
    require("ausgelassen" in kurz, "der Beschnitt wird verschwiegen")
    voll = E.diff_text(repo, basis, max_chars=10_000_000)
    require("ausgelassen" not in voll, "ein vollstaendiger Diff meldete Beschnitt")


def t_git_helpers_read_the_real_tree() -> None:
    repo = _repo()
    require(not E.is_dirty(repo), "ein frisches Repo galt als schmutzig")
    with open(os.path.join(repo, "unsauber.txt"), "w") as fh:
        fh.write("x")
    require(E.is_dirty(repo), "eine ungetrackte Datei wurde nicht bemerkt")
    exc = require_raises(E.EvidenceError, E.head_commit,
                         tempfile.mkdtemp(dir=_SANDBOX))
    require_equal(exc.reason, "git_failed", "ein Nicht-Repo lieferte einen Commit")


def t_a_red_gate_names_which_suites_were_red() -> None:
    """Live gelernt in der A7-Abnahme: im Buch stand „48 rot" und nirgends WO.

    Die Nachfrage kostete einen zweiten zwoelfminuetigen Gate-Lauf. Eine
    Messung, die den Befund nicht mitnennt, zwingt zur Wiederholung.
    """
    text = ("test_alpha.py    10    10     9     1     0   <<< FAIL\n"
            "test_beta.py     20    20    20     0     0\n"
            "test_gamma.py     5     5     3     2     0   <<< FAIL\n"
            "Suites: 3   EXPECTED=35 EXECUTED=35 PASSED=32 FAILED=3 SKIPPED=0\n"
            "Missing=0 BaselineDrift=0")
    r = E.parse_gate(text, 1)
    require_equal(sorted(r.failing_suites), ["test_alpha.py", "test_gamma.py"],
                  f"die roten Suiten fehlen: {r.failing_suites}")
    require("test_alpha.py" in r.summary(),
            f"die Zusammenfassung nennt sie nicht: {r.summary()}")
    require("test_beta.py" not in r.summary(),
            "eine gruene Suite wurde als rot genannt")
    require("failing_suites" in r.as_dict(),
            "die Evidence-Nutzlast traegt sie nicht")

    gruen = E.parse_gate(GRUEN, 0)
    require_equal(gruen.failing_suites, (),
                  "ein gruenes Gate nannte rote Suiten")


def t_the_named_suites_survive_evidence_reuse() -> None:
    """Sonst waere die Auskunft beim zweiten Blick verschwunden."""
    led, mid = _welt()
    repo = _repo()
    stand = E.head_commit(repo)
    fp = E.environment_fingerprint(repo)
    led.record_evidence(mid, kind="test_report", commit=stand,
                        env_fingerprint=fp, ok=False, summary="rot",
                        payload={"failing_suites": ["test_alpha.py"],
                                 "failed": 1, "executed": 10, "passed": 9})
    _eid, ergebnis, wieder = E.record_gate(led, mid, repo, commit=stand)
    require(wieder, "die Messung wurde nicht wiederverwendet")
    require_equal(list(ergebnis.failing_suites), ["test_alpha.py"],
                  f"die roten Suiten gingen verloren: {ergebnis.failing_suites}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
