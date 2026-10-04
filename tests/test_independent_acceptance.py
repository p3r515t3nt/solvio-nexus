"""Die unabhaengige Abnahme — gemessen ausserhalb dessen, was der Builder aendert.

**Der Befund, den diese Suite absichert (DEBT-0234).** Der Autopilot misst sein
Gate als `scripts/run_tests.py` AUS DEM ARBEITSBEREICH. Der Kaefig erlaubt dem
Builder Schreiben im ganzen Arbeitsbereich; die einzige nachgelagerte Pruefung
sucht Kredentialgestalt, keine geschuetzten Pfade. Der Gepruefte stellte also
die Pruefung — und im echten Lauf `gap-1f983ef8cf3ed38c` hat ein Builder
`scripts/run_tests.py` tatsaechlich veraendert (Checkpoint `b5e5358a`,
verschaerfend; derselbe Weg trug auch in die andere Richtung).

Die Suite prueft VERHALTEN an einem echten kleinen git-Repository: sie legt
Commits an, aendert Dateien, faehrt die Abnahme und liest, was im Buch steht.
Kein Modell, kein Netz, kein Builder, keine Produktionsdaten.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-independent-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["SOLVIO_AUTOPILOT_DB"] = os.path.join(_SANDBOX, "autopilot.sqlite3")

from solvio.autopilot import independent as IND                 # noqa: E402
from solvio.autopilot import store as A                          # noqa: E402

#: Ein Gate, das ECHT misst: es liest eine Datei und entscheidet danach. So
#: kann eine Manipulation der Pruefinfrastruktur wirklich stattfinden statt
#: nur behauptet zu werden.
GATE_ECHT = """import os
_p = os.path.join(os.path.dirname(__file__), '..', 'src', 'werk.py')
try:
    _inhalt = open(_p, encoding='utf-8').read()
except OSError:
    _inhalt = ''
gruen = 'KORREKT' in _inhalt
print('Suites: 1   EXPECTED=1 EXECUTED=1 PASSED=%d FAILED=%d SKIPPED=0'
      % ((1, 0) if gruen else (0, 1)))
print('Missing=0 BaselineDrift=0')
raise SystemExit(0 if gruen else 1)
"""

#: Dasselbe Gate, nur dass es nichts mehr prueft. Genau die Aenderung, die ein
#: Builder machen koennte, um sein Examen zu bestehen, ohne die Aufgabe zu tun.
GATE_ENTSCHAERFT = """print('Suites: 1   EXPECTED=1 EXECUTED=1 PASSED=1 FAILED=0 SKIPPED=0')
print('Missing=0 BaselineDrift=0')
raise SystemExit(0)
"""


def _messen(buch, m, repo):
    """`IND.measure` mit EIGENEM Arbeitsordner.

    Ohne `scratch` legt `measure` seinen Pruefbaum unter `~/.solvio/independent`
    an — im PRODUKTIVEN Zustand. Genau das ist beim ersten Full Gate passiert
    und wurde von `test_inventory_reconciliation` gefunden: ein unbekannter
    Eintrag in `~/.solvio`. Die Suite hat ihren Sandkasten; sie benutzt ihn
    auch. (DEBT-0223 ist dieselbe Klasse.)
    """
    return IND.measure(buch, m, repo, python=sys.executable,
                       scratch=os.path.join(_SANDBOX, "independent"))


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(*, code="KORREKT ist der Anfangszustand") -> str:
    """Ein echtes kleines Projekt mit Gate, Test und Code."""
    repo = tempfile.mkdtemp(prefix="proj-", dir=_SANDBOX)
    for ordner in ("scripts", "src", "tests"):
        os.makedirs(os.path.join(repo, ordner), exist_ok=True)
    with open(os.path.join(repo, "scripts", "run_tests.py"), "w") as fh:
        fh.write(GATE_ECHT)
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write(f'"""{code}"""\n')
    with open(os.path.join(repo, "tests", "test_werk.py"), "w") as fh:
        fh.write("def test_werk():\n    assert True\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@solvio.local", "-c", "user.name=t",
         "commit", "-q", "-m", "Basis")
    return os.path.realpath(repo)


def _commit(repo, nachricht="Bau"):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@solvio.local", "-c", "user.name=t",
         "commit", "-q", "-m", nachricht)
    return _git(repo, "rev-parse", "HEAD")


def _buch_mit_basis(repo, basis) -> tuple:
    """Ein Buch, dessen erste Bauphase die unberuehrte Basis traegt."""
    buch = A.AutopilotLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                          "autopilot.sqlite3"))
    from solvio.autopilot import contract as C
    vertrag = C.Contract(milestone_id="m-unab", version="1.0.0",
                         objective="Etwas bauen",
                         acceptance_criteria=(
                             C.AcceptanceCriterion(key="gate_green",
                                                   text="Gate gruen",
                                                   evidence_type=C.DETERMINISTIC),),
                         repository=repo)
    buch.create_milestone(vertrag)
    buch.start_phase("m-unab", kind="build", builder="test", base_commit=basis)
    return buch, "m-unab"


# =====================================================================
# A — Der Anker: WELCHE Basis gilt
# =====================================================================

def t_the_base_comes_from_the_first_build_phase():
    """**Die Basis ist die der ERSTEN Bauphase — nicht die zuletzt gesetzte.**

    `driver` ueberschreibt `milestones.base_commit` vor jeder Baurunde mit dem
    aktuellen Arbeitsbereichskopf. Ab Runde zwei ist das ein Stand, den der
    Builder selbst erzeugt hat. Wer den nimmt, misst gegen das Werk des
    Gepruesten und merkt es nicht.
    """
    repo = _repo()
    erste = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, erste)
    with open(os.path.join(repo, "src", "werk.py"), "a") as fh:
        fh.write("# Runde eins\n")
    zweite = _commit(repo)
    buch.start_phase(m, kind="build", builder="test", base_commit=zweite)

    require_equal(IND.original_base(buch, m), erste,
                  "die spaetere Basis hat gewonnen — die Abnahme misst gegen "
                  "den Builder selbst")


def t_without_a_build_phase_there_is_no_base():
    """Ohne Bauphase gibt es keine Basis — und keine stillschweigende Ersatzbasis."""
    repo = _repo()
    buch = A.AutopilotLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                          "a.sqlite3"))
    from solvio.autopilot import contract as C
    buch.create_milestone(C.Contract(
        milestone_id="m-leer", version="1.0.0", objective="x",
        acceptance_criteria=(C.AcceptanceCriterion(
            key="gate_green", text="g", evidence_type=C.DETERMINISTIC),),
        repository=repo))
    geworfen = ""
    try:
        IND.original_base(buch, "m-leer")
    except IND.IndependentError as exc:
        geworfen = exc.reason
    require_equal(geworfen, "no_base_phase", "eine Basis entstand aus dem Nichts")


# =====================================================================
# B — Die Verweigerung: wer sein Examen anfasst, wird nicht gemessen
# =====================================================================

def t_a_weakened_gate_is_refused_not_measured():
    """**Der Kern.** Ein entschaerftes Gate darf nichts beweisen.

    Der Builder aendert `scripts/run_tests.py` so, dass es immer gruen meldet,
    und liefert Code, der die Aufgabe NICHT erfuellt. Im Arbeitsbereich waere
    das gruen. Die unabhaengige Abnahme misst gar nicht erst — sie verweigert,
    und die Verweigerung steht als `ok=False` im Buch.
    """
    repo = _repo()
    basis = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, basis)
    with open(os.path.join(repo, "scripts", "run_tests.py"), "w") as fh:
        fh.write(GATE_ENTSCHAERFT)                      # das Examen entschaerft
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write('"""Die Aufgabe wurde nicht getan."""\n')
    _commit(repo)

    beleg, ergebnis, befund = _messen(buch, m, repo)
    require(ergebnis is None, "es wurde trotzdem gemessen")
    require_equal(befund["reason"], "examination_modified", befund)
    gebucht = buch.evidence(beleg)
    require(not gebucht.ok, "eine Verweigerung wurde als bestanden gebucht")
    require_equal(gebucht.kind, "independent_gate")
    require("run_tests.py" in gebucht.payload_json, gebucht.payload_json[:200])


def t_a_deleted_test_is_refused_too():
    """Loeschen ist Aendern. Wer eine Suite entfernt, aendert sein Examen."""
    repo = _repo()
    basis = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, basis)
    os.remove(os.path.join(repo, "tests", "test_werk.py"))
    _commit(repo)

    _beleg, ergebnis, befund = _messen(buch, m, repo)
    require(ergebnis is None, "eine geloeschte Suite wurde einfach mitgemessen")
    require_equal(befund["reason"], "examination_modified", befund)


def t_a_new_test_is_allowed_but_never_runs_in_the_examination():
    """**Die Gegenrichtung, die halten muss.**

    Eine neue Faehigkeit braucht neue Tests — sonst waere die Abnahme eine
    Sperre gegen jeden Fortschritt. Neue Testdateien sind deshalb erlaubt. Sie
    wandern aber NICHT in den Pruefbaum: wer geprueft wird, stellt die
    Pruefung nicht.
    """
    repo = _repo()
    basis = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, basis)
    with open(os.path.join(repo, "tests", "test_neu.py"), "w") as fh:
        fh.write("def test_neu():\n    assert True\n")
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write('"""KORREKT — die Aufgabe wurde getan."""\n')
    kopf = _commit(repo)

    beleg, ergebnis, befund = _messen(buch, m, repo)
    require(ergebnis is not None, f"die Abnahme verweigerte: {befund}")
    require(ergebnis.ok, f"die Abnahme wurde rot: {ergebnis.reason}")
    require_equal(befund["examination_added"], ["tests/test_neu.py"], befund)

    # Und der neue Test lag beim Messen NICHT im Pruefbaum.
    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    IND.build_tree(repo, base=basis, head=kopf, ziel=ordner)
    require(not os.path.exists(os.path.join(ordner, "tests", "test_neu.py")),
            "der Test des Gepruesten lag im Pruefbaum")
    require(os.path.exists(os.path.join(ordner, "tests", "test_werk.py")),
            "der urspruengliche Test fehlt im Pruefbaum")


# =====================================================================
# C — Die Messung selbst
# =====================================================================

def t_the_original_tests_judge_the_new_code():
    """**Was die Abnahme positiv leistet.** Basis-Tests, neuer Code.

    Der Builder erfuellt die Aufgabe und fasst das Examen nicht an. Die
    urspruenglichen Tests laufen gegen seinen Code und werden gruen.
    """
    repo = _repo(code="NOCH NICHT")
    basis = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, basis)
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write('"""KORREKT"""\n')
    _commit(repo)

    beleg, ergebnis, _b = _messen(buch, m, repo)
    require(ergebnis is not None and ergebnis.ok,
            f"die richtige Loesung wurde nicht anerkannt: "
            f"{ergebnis and ergebnis.reason}")
    require(buch.evidence(beleg).ok, "der Beleg wurde rot gebucht")


def t_code_that_fails_the_original_tests_stays_red():
    """Die Gegenprobe dazu: falscher Code, unveraendertes Examen, rot."""
    repo = _repo(code="NOCH NICHT")
    basis = _git(repo, "rev-parse", "HEAD")
    buch, m = _buch_mit_basis(repo, basis)
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write('"""IMMER NOCH NICHT"""\n')
    _commit(repo)

    _beleg, ergebnis, _b = _messen(buch, m, repo)
    require(ergebnis is not None, "es wurde gar nicht gemessen")
    require(not ergebnis.ok, "falscher Code hat die Basis-Tests bestanden")


def t_deleted_code_disappears_from_the_examined_tree():
    """Geloeschter Code fehlt auch in der Kopie — sonst pruefte sie ein Phantom."""
    repo = _repo()
    basis = _git(repo, "rev-parse", "HEAD")
    with open(os.path.join(repo, "src", "zusatz.py"), "w") as fh:
        fh.write("x = 1\n")
    _commit(repo, "Zusatz")
    mit = _git(repo, "rev-parse", "HEAD")
    os.remove(os.path.join(repo, "src", "zusatz.py"))
    ohne = _commit(repo, "Zusatz fort")

    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    IND.build_tree(repo, base=mit, head=ohne, ziel=ordner)
    require(not os.path.exists(os.path.join(ordner, "src", "zusatz.py")),
            "geloeschter Code stand noch im Pruefbaum")


def t_the_examined_tree_is_not_the_workspace():
    """Der Pruefbaum ist eine KOPIE. Ein Schreiben dort beruehrt den Bau nicht —
    und umgekehrt kann der Bau die laufende Messung nicht mehr aendern."""
    repo = _repo()
    basis = _git(repo, "rev-parse", "HEAD")
    with open(os.path.join(repo, "src", "werk.py"), "w") as fh:
        fh.write('"""KORREKT"""\n')
    kopf = _commit(repo)
    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    IND.build_tree(repo, base=basis, head=kopf, ziel=ordner)
    require(os.path.realpath(ordner) != os.path.realpath(repo),
            "der Pruefbaum IST der Arbeitsbereich")
    with open(os.path.join(ordner, "src", "werk.py"), "w") as fh:
        fh.write("veraendert in der Kopie\n")
    require("KORREKT" in open(os.path.join(repo, "src", "werk.py")).read(),
            "eine Aenderung in der Kopie hat den Arbeitsbereich getroffen")


# =====================================================================
# D — Die Naht zum Treiber
# =====================================================================

def t_the_driver_proves_a_gate_criterion_only_with_the_independent_run():
    """**Die eigentliche Zusage am Treiber.**

    `_apply` darf ein DETERMINISTIC-Gate-Kriterium nur beweisen, wenn die
    UNABHAENGIGE Messung gruen ist — und die Referenz muss auf sie zeigen,
    nicht auf das Gate des Arbeitsbereichs. Geprueft am Verhalten: dieselbe
    Lage einmal mit und einmal ohne unabhaengigen Beleg.
    """
    from solvio.autopilot import driver as D
    from solvio.autopilot import lead as LEAD

    repo = _repo()
    faelle = []
    for name, unabhaengig_ok in (("ohne Abnahme", None), ("rote Abnahme", False),
                                 ("gruene Abnahme", True)):
        buch, m = _buch_mit_basis(repo, _git(repo, "rev-parse", "HEAD"))
        gate = buch.record_evidence(m, kind="test_report", commit="c1",
                                    env_fingerprint="f", ok=True, summary="gruen")
        treiber = D.Driver.__new__(D.Driver)
        treiber.ledger = buch
        treiber._independent = ""
        if unabhaengig_ok is not None:
            treiber._independent = buch.record_evidence(
                m, kind="independent_gate", commit="c1", env_fingerprint="f",
                ok=unabhaengig_ok, summary="unabhaengig")
        D.Driver._apply(treiber, m, LEAD.Verdict(
            verdict=LEAD.READY, next_action="release"), gate_evidence=gate)
        k = [c for c in buch.criteria(m) if c["key"] == "gate_green"][0]
        faelle.append((name, k["state"], k["evidence_ref"]))

    require_equal(faelle[0][1], A.CRIT_OPEN,
                  "ohne unabhaengige Abnahme wurde bewiesen")
    require_equal(faelle[1][1], A.CRIT_OPEN,
                  "eine rote unabhaengige Abnahme hat bewiesen")
    require_equal(faelle[2][1], A.CRIT_PROVEN,
                  "eine gruene unabhaengige Abnahme hat nicht bewiesen")
    require(faelle[2][2].startswith("ev-"), faelle[2])
    require_equal(faelle[2][2], treiber._independent,
                  "der Beleg zeigt auf das Gate des Arbeitsbereichs, nicht auf "
                  "die unabhaengige Messung")


def t_the_examination_list_covers_what_the_gate_actually_reads():
    """Die Liste der Pruefinfrastruktur ist nicht geraten.

    Sie deckt genau die Pfade, ueber die ein Builder die Messung beeinflussen
    kann: die Suiten, den Laeufer, den Arbeiter und die Baseline. Faellt einer
    heraus, ist die Verweigerung loechrig.
    """
    for pfad in ("tests/test_irgendwas.py", "tests/_inventory.py",
                 "tests/test_inventory_baseline.json", "scripts/run_tests.py",
                 "scripts/_test_worker.py", "scripts/update_test_baseline.py"):
        require(IND.is_examination(pfad), f"{pfad} gilt nicht als Examen")
    for pfad in ("src/solvio/agent_runtime/store.py", "README.md",
                 "scripts/development_live.py", "docs/x.md"):
        require(not IND.is_examination(pfad), f"{pfad} gilt faelschlich als Examen")


def t_a_forgotten_scratch_never_writes_into_production_state():
    """**Ein vergessener Parameter darf nicht in Produktion schreiben.**

    Die erste Fassung legte den Pruefbaum fest unter `~/.solvio/independent`
    an. Diese Suite gab `scratch` nicht mit — und schrieb damit ein Verzeichnis
    in den PRODUKTIVEN Zustand. Gefunden hat das nicht ich, sondern das erste
    Full Gate: `test_inventory_reconciliation` meldete einen unbekannten
    Eintrag in `~/.solvio`. Dieselbe Klasse wie DEBT-0223.

    Der Arbeitsordner folgt jetzt dem BUCH. Wer `SOLVIO_AUTOPILOT_DB` umleitet,
    leitet ihn mit um — ohne an einen zweiten Schalter zu denken.
    """
    buch = A.AutopilotLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                          "autopilot.sqlite3"))
    wurzel = IND._scratch_root(buch)
    produktiv = os.path.realpath(os.path.expanduser("~/.solvio"))
    require(not os.path.realpath(wurzel).startswith(produktiv),
            f"der Arbeitsordner zeigt in den produktiven Zustand: {wurzel}")
    require(os.path.realpath(wurzel).startswith(os.path.realpath(_SANDBOX)),
            f"der Arbeitsordner folgt dem Buch nicht: {wurzel}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
