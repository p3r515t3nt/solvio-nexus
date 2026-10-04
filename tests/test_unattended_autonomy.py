"""Die zwei Dinge, die unbeaufsichtigte Arbeit bisher angehalten haben.

Beide sind keine Funktionen, sondern Sperren — und beide hielten den Autopiloten
an einer Stelle an, an der nichts falsch war.

**P0.1 — der Checkpoint-Scan las die Datei statt der Aenderung.** Er
beantwortete „steht irgendwo in dieser Datei etwas Kredentialfoermiges?" und
nicht „hat dieser Schritt Anmeldematerial hineingelegt?". Datei fuer Datei
nachgemessen waren 25 von 474 verfolgten Dateien damit unberuehrbar — darunter
Suiten, die Wegwerf-Werte ALS TESTDATEN tragen muessen. Eine Bauphase endete
daran, ohne dass der Builder eine einzige kredentialfoermige Zeile geschrieben
hatte (DEBT-0189).

**P0.2 — `/usr/bin/git` ist auf macOS kein git.** Es ist der xcselect-
Weiterleiter; darf er das Entwicklerverzeichnis nicht lesen, haelt er die
Werkzeuge fuer nicht installiert und oeffnet den GUI-Installer. Fuenf
erfolgreiche Installationen an einem Tag halfen nichts, weil nie etwas fehlte.

Was hier NICHT geprueft wird: ob die Muster gut sind. Sie bleiben unveraendert
— keine Abschwaechung, keine Whitelist, kein genereller Ausschluss von
Produktionsmodulen oder Suiten. Geprueft wird die Unterscheidung.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_unattended_autonomy.py
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

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-unattended-")
atexit.register(shutil.rmtree, _SANDBOX, True)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

from solvio import git_binary as GB           # noqa: E402
from solvio.autopilot import builders as B    # noqa: E402
from solvio.autopilot import publisher as P   # noqa: E402

#: Zusammengesetzt statt hingeschrieben. Die Suite selbst soll keine Zeile
#: tragen, die wie ein Zugang aussieht — auch dann nicht, wenn genau dieser
#: Milestone das wieder erlaubt.
FAKE = "sk-ant-oat-" + "SYNTHETISCH-NIEMALS-ECHT-0001"

_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example"}


def _git(repo, *args, check=True):
    return subprocess.run([GB.resolve(), "-C", repo, *args],
                          env={**os.environ, **_ENV}, capture_output=True,
                          text=True, check=check)


def _schreibe(repo, rel, text):
    voll = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(voll), exist_ok=True)
    with open(voll, "w", encoding="utf-8") as fh:
        fh.write(text)


#: Eine Suite, wie es sie im Repository wirklich gibt: ein Wegwerf-Wert als
#: Testdatum, umgeben von gewoehnlichem Code.
BEKANNTE_SUITE = (
    "import os\n"
    "\n"
    f'TESTWERT = "{FAKE}"\n'
    "\n"
    "def t_etwas():\n"
    "    assert TESTWERT\n"
)


def _repo_mit_bekannter_suite() -> str:
    """Ein Repo, in dem der kredentialfoermige Wert BEREITS Bestandteil ist."""
    repo = tempfile.mkdtemp(dir=_SANDBOX)
    _git(repo, "init", "-q", "-b", "main")
    _schreibe(repo, "tests/suite.py", BEKANNTE_SUITE)
    _schreibe(repo, "modul.py", "def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "basis")
    return repo


# =====================================================================
# P0.1 — der Checkpoint prueft die Aenderung
# =====================================================================

def t_p01_an_existing_test_value_stays_touchable() -> None:
    """1. Vorhandener credential-artiger Testwert, unveraendert → erlaubt.

    Das ist der ganze Zweck. Die Datei traegt den Wert weiterhin; der Schritt
    hat ihn nicht hineingelegt, sondern eine Zeile daneben geschrieben.
    """
    repo = _repo_mit_bekannter_suite()
    with open(os.path.join(repo, "tests/suite.py"), "a", encoding="utf-8") as fh:
        fh.write("\ndef t_noch_etwas():\n    assert True\n")

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(ergebnis.ok,
            f"eine bekannte Suite blieb unberuehrbar: {ergebnis.reason} "
            f"{ergebnis.findings}")
    # Und der Wert steht noch da — es wurde nichts stillschweigend entfernt.
    text = open(os.path.join(repo, "tests/suite.py"), encoding="utf-8").read()
    require(FAKE in text, "der vorhandene Testwert verschwand aus der Datei")


def t_p01_a_new_credential_line_in_a_known_file_is_refused() -> None:
    """2. Builder fuegt eine neue credential-artige Zeile hinzu → verweigert.

    Die Gegenprobe zur ersten Zusicherung: dieselbe bekannte Datei, aber
    diesmal ist der Zugang NEU. Ohne diesen Fall waere die erste Zusicherung
    auch mit einem Scan zufrieden, der gar nichts mehr prueft.
    """
    repo = _repo_mit_bekannter_suite()
    with open(os.path.join(repo, "tests/suite.py"), "a", encoding="utf-8") as fh:
        fh.write(f'\nZWEITER = "sk-ant-oat-EIN-ZWEITER-WERT-0002"\n')

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok, "eine neu hinzugefuegte Zugangszeile kam durch")
    require_equal(ergebnis.reason, "credential_in_checkpoint",
                  f"falscher Grund: {ergebnis.reason}")
    require(any("tests/suite.py" in f for f in ergebnis.findings),
            f"der Fund nennt die Datei nicht: {ergebnis.findings}")


def t_p01_a_new_file_with_a_credential_is_refused() -> None:
    """3. Neue Datei mit Credential → verweigert."""
    repo = _repo_mit_bekannter_suite()
    _schreibe(repo, "neu.py", f'TOKEN = "{FAKE}"\n')

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok, "eine neue Datei mit Zugang kam durch")
    require(any("neu.py" in f for f in ergebnis.findings),
            f"der Fund nennt die neue Datei nicht: {ergebnis.findings}")


def t_p01_a_copied_file_with_a_credential_is_refused() -> None:
    """4. Kopierte Datei mit Credential → verweigert.

    Der Inhalt stand schon im Repository — und ist trotzdem an dieser Stelle
    neu. Wer nur fragt „kannte das Repository diese Zeichen?", laesst ihn
    durch.
    """
    repo = _repo_mit_bekannter_suite()
    shutil.copyfile(os.path.join(repo, "tests/suite.py"),
                    os.path.join(repo, "kopie.py"))

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok, "eine Kopie mit Zugang kam durch")
    require(any("kopie.py" in f for f in ergebnis.findings),
            f"der Fund nennt die Kopie nicht: {ergebnis.findings}")


def t_p01_a_rename_heuristic_cannot_hide_new_content() -> None:
    """4b. Dieselbe Lage, aber so gestellt, dass git eine Umbenennung SIEHT.

    Wird das Original geloescht und der Inhalt unter neuem Namen angelegt,
    paart git beides zu `R100` — und eine Aenderung mit hundert Prozent
    Aehnlichkeit hat keine hinzugefuegten Zeilen. Genau dort koennte neuer
    Inhalt als alter erscheinen. Deshalb ist die Erkennung aus.
    """
    repo = _repo_mit_bekannter_suite()
    shutil.move(os.path.join(repo, "tests/suite.py"),
                os.path.join(repo, "tests/umbenannt.py"))

    # Erst die Messung: git WUERDE das als Umbenennung erkennen.
    _git(repo, "add", "-A")
    mit = _git(repo, "diff", "--cached", "--find-renames", "--name-status")
    require("R" in mit.stdout.split()[0] if mit.stdout.split() else False,
            f"die Lage stellt keine Umbenennung: {mit.stdout!r}")
    _git(repo, "reset", "--quiet", "HEAD", check=False)

    # Der scharfe Punkt: der Scan darf sie NICHT als Umbenennung sehen. Das
    # Endergebnis allein ist stumpf — ein unbekannter Status faellt ohnehin auf
    # die strenge Seite, und damit waere der Fall auch mit Erkennung rot.
    _git(repo, "add", "-A")
    lage = B._change_status(repo, None)
    require_equal(lage.get("tests/umbenannt.py"), "A",
                  f"der neue Name gilt nicht als neu: {lage}")
    _git(repo, "reset", "--quiet", "HEAD", check=False)

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok,
            "eine Umbenennung trug den Zugang an der Pruefung vorbei")
    require(any("umbenannt.py" in f for f in ergebnis.findings),
            f"der Fund nennt den neuen Namen nicht: {ergebnis.findings}")


def t_p01_a_secret_re_added_inside_a_known_file_is_treated_safely() -> None:
    """5. Secret innerhalb vorhandener Datei verschoben/re-added → sicher.

    „Sicher" heisst hier: streng. Eine verschobene Zeile ist im Diff eine
    hinzugefuegte Zeile, und der Scan behandelt sie wie jede andere. Das ist
    die Richtung, in die dieser Riegel irren darf.
    """
    repo = _repo_mit_bekannter_suite()
    zeilen = BEKANNTE_SUITE.splitlines()
    wert = [z for z in zeilen if FAKE in z][0]
    rest = [z for z in zeilen if FAKE not in z]
    _schreibe(repo, "tests/suite.py", "\n".join(rest + ["", wert]) + "\n")

    ergebnis = B.safe_checkpoint(repo, phase="build")
    require(not ergebnis.ok,
            "eine erneut eingefuegte Zugangszeile galt als bekannt")
    require(any("tests/suite.py" in f for f in ergebnis.findings),
            f"der Fund nennt die Datei nicht: {ergebnis.findings}")


def t_p01_a_path_git_says_nothing_about_is_read_whole() -> None:
    """Der unbekannte Fall faellt auf die strenge Seite.

    Der Scan bekommt seine Pfadliste von git und fragt git nach dem Status.
    Sagt git zu einem Pfad nichts — weil eine Liste veraltet ist, weil jemand
    sie durchreicht, weil ein Aufrufer sich irrt —, dann ist „unbekannt" kein
    Freibrief. Ohne diese Zusicherung waere `lage.get(rel, "M")` eine
    einzeilige, unsichtbare Oeffnung: unbekannt = geaendert = leerer Diff =
    sauber.
    """
    repo = _repo_mit_bekannter_suite()
    _schreibe(repo, "unbeachtet.py", f'TOKEN = "{FAKE}"\n')
    # Absichtlich NICHT vorgemerkt: git sagt in `diff --cached` nichts darueber.
    funde = list(B._scan_staged(repo, ["unbeachtet.py"]))
    require(any("unbeachtet.py" in f for f in funde),
            f"ein Pfad ohne Status kam ungeprueft durch: {funde}")


def t_p01_the_repo_wide_secret_gate_stays_independent_and_effective() -> None:
    """6. Der echte repo-weite Secret-Scan bleibt wirksam.

    Er ist eine andere Frage an eine andere Menge: nicht „was hat dieser
    Schritt getan?", sondern „steht in der Wissensbasis ein Wert, der dort
    nie stehen darf?". Diese Lockerung darf ihn nicht beruehren.
    """
    import _public_document_secrets as PK
    import re

    # Er laeuft, und er ist heute gruen.
    PK.check_public_documents()

    # Er wuerde einen Zugang auch finden — die Muster sind nicht leer geworden.
    probe = f'api_key = "{FAKE}"'
    treffer = [was for muster, was in PK.SECRETS if re.search(muster, probe)]
    require(treffer, "der repo-weite Scan erkennt nichts mehr")

    # Und er haengt nicht am Checkpoint-Scan. Waere er es, waere er keine
    # unabhaengige zweite Meinung.
    quelle = open(os.path.join(REPO, "tests", "_public_document_secrets.py"),
                  encoding="utf-8").read()
    require("_scan_staged" not in quelle and "safe_checkpoint" not in quelle,
            "der repo-weite Scan haengt am Checkpoint-Scan")


def t_p01_the_publisher_seam_makes_the_same_distinction() -> None:
    """Die zweite Stelle mit demselben Scan — ueber einem Commit-Bereich.

    Sie ist der Riegel vor dem kanonischen Repository. Waere hier die alte
    Semantik geblieben, waere der Checkpoint frei und die Veroeffentlichung
    weiterhin blockiert — also nichts gewonnen.
    """
    repo = _repo_mit_bekannter_suite()

    # a) Harmlose Aenderung an der bekannten Suite.
    with open(os.path.join(repo, "tests/suite.py"), "a", encoding="utf-8") as fh:
        fh.write("\n# eine Bemerkung\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "harmlos")
    kopf = _git(repo, "rev-parse", "HEAD").stdout.strip()
    require_equal(P._default_scanner(repo, kopf), [],
                  "die Veroeffentlichung haelt eine harmlose Aenderung auf")

    # b) Neue Zugangszeile im selben Baum.
    with open(os.path.join(repo, "modul.py"), "a", encoding="utf-8") as fh:
        fh.write('\nGEHEIM = "sk-ant-oat-DRITTER-WERT-0003"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "zugang")
    kopf = _git(repo, "rev-parse", "HEAD").stdout.strip()
    funde = P._default_scanner(repo, kopf)
    require(any("modul.py" in f for f in funde),
            f"die Veroeffentlichung liess einen neuen Zugang durch: {funde}")


# =====================================================================
# P0.2 — git ohne GUI-Dialog
# =====================================================================

def t_p02_the_forwarder_is_recognised_and_never_chosen() -> None:
    """Der Weiterleiter wird erkannt — und ist nie die gewaehlte Binary."""
    if not GB.is_forwarder("/usr/bin/git"):
        return  # kein macOS-Weiterleiter auf diesem System
    require_equal(GB.why_not("/usr/bin/git"), "xcselect_forwarder",
                  "der Weiterleiter faellt nicht mit dem richtigen Grund durch")
    gewaehlt = GB.resolve()
    require(gewaehlt != "/usr/bin/git",
            "die unbeaufsichtigte Arbeit ruft den Weiterleiter")
    require(not GB.is_forwarder(gewaehlt),
            f"die gewaehlte Binary ist ein Weiterleiter: {gewaehlt}")


def t_p02_the_check_never_runs_the_forwarder() -> None:
    """Die Reihenfolge IST der Schutz.

    Stuende die Signaturpruefung nach dem Probelauf, wuerde die Pruefung selbst
    den Dialog oeffnen, den sie verhindern soll. Hier laeuft eine Datei mit
    der Signatur, die eine Spur hinterlaesst, wenn sie ausgefuehrt wird.
    """
    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    spur = os.path.join(ordner, "wurde-ausgefuehrt")
    falsch = os.path.join(ordner, "git")
    with open(falsch, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 f"touch {spur}\n"
                 "echo 'git version 9.9.9'\n"
                 "# libxcselect.dylib\n")
    os.chmod(falsch, 0o755)

    require(GB.is_forwarder(falsch), "die Signatur wird nicht gefunden")
    require_equal(GB.why_not(falsch), "xcselect_forwarder",
                  "der Grund ist nicht die Signatur")
    require(not os.path.exists(spur),
            "die Pruefung hat den Weiterleiter ausgefuehrt")


def t_p02_the_resolved_binary_runs_in_a_bare_child_process() -> None:
    """Ohne PATH, ohne geerbte Umgebung, in einem frischen Kindprozess.

    Genau die Lage eines launchd-Auftrags — und genau die, in der `subprocess`
    ohne PATH auf `/bin:/usr/bin` zurueckfaellt und damit frueher beim
    Weiterleiter landete.
    """
    binaer = GB.resolve()
    proc = subprocess.run([binaer, "--version"], env={}, capture_output=True,
                          text=True, timeout=30, check=False)
    require_equal(proc.returncode, 0, f"git lief nicht: {proc.stderr[:200]}")
    require(proc.stdout.startswith("git version"),
            f"das ist kein git: {proc.stdout[:80]!r}")

    # Und es arbeitet wirklich, nicht nur `--version`.
    repo = _repo_mit_bekannter_suite()
    kopf = subprocess.run([binaer, "-C", repo, "rev-parse", "HEAD"], env={},
                          capture_output=True, text=True, timeout=30,
                          check=False)
    require_equal(kopf.returncode, 0, f"rev-parse scheiterte: {kopf.stderr[:200]}")
    require(len(kopf.stdout.strip()) == 40, f"kein Commit: {kopf.stdout!r}")


def t_p02_the_builder_jail_reaches_a_real_git() -> None:
    """Der Kaefig, in dem der Dialog wirklich entstand.

    Das Seatbelt-Profil des Builders erlaubte `/usr` und `/bin`, aber kein
    Entwicklerverzeichnis. Der Weiterleiter unter `/usr/bin/git` fand dort
    nichts — genau das hat der Kernel am 2026-09-02 protokolliert, waehrend der
    Installer-Dialog lief.

    **Die Gegenprobe wird STRUKTURELL gestellt, nicht ausgefuehrt.** Ein
    Profil ohne Entwicklerverzeichnis lief in `xcode-select: note: No developer
    tools were found, requesting install.` — und „requesting install" ist keine
    Meldung, sondern eine Handlung: der Dialog geht auf. Einmal beim Schaerfen
    dieser Suite passiert, einmal geschlossen, einmal gelernt. Eine Zusicherung,
    die das Problem vorfuehrt, ist genau das Problem.
    """
    from solvio.agent_runtime import isolation as I
    from solvio.specialists import launcher as L

    ws = tempfile.mkdtemp(dir=_SANDBOX)
    kaefig = tempfile.mkdtemp(dir=_SANDBOX)
    profil = I.render_profile(workspace=ws, scratch=kaefig)

    wurzel = I.trusted_git_root()
    require(wurzel, "der Kaefig kennt keine gepruefte git-Wurzel")
    require(wurzel in profil,
            f"das Profil nennt die gepruefte Werkzeugkette nicht: {wurzel}")
    require(GB.resolve().startswith(wurzel + os.sep),
            f"die Wurzel {wurzel} traegt die gewaehlte Binary nicht")

    # Die Gegenprobe: ohne diese eine Zeile stuende sie nicht drin. Damit haengt
    # die Zusicherung an der Aenderung und nicht an einem Zufall des Profils.
    ohne = I.render_profile(workspace=ws, scratch=kaefig,
                            toolchain=("/usr", "/bin"))
    if not wurzel.startswith(("/usr", "/bin")):
        require(wurzel in ohne,
                "die Wurzel kommt nicht aus dem Aufruf, sondern aus der Liste")

    # Und die versiegelten Pfade bleiben draussen — eine Erlaubnis mehr ist
    # die Stelle, an der man aus Versehen eine zweite mitnimmt.
    require_equal(I.sealed_violations(profil), [],
                  "das erweiterte Profil nennt einen versiegelten Pfad")

    # Positiv, im echten Kaefig: git laeuft und ist kein Weiterleiter.
    if not os.path.exists("/usr/bin/sandbox-exec"):
        return
    pfad = os.path.join(kaefig, "builder.sandbox.sb")
    with open(pfad, "w", encoding="utf-8") as fh:
        fh.write(profil)
    umgebung = L.child_environment()
    lauf = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", pfad, "/bin/sh", "-c",
         "command -v git && git --version"],
        cwd=ws, env=umgebung, capture_output=True, text=True, timeout=60,
        check=False)
    ausgabe = lauf.stdout + lauf.stderr
    require("developer tools" not in ausgabe and "developer directory" not in ausgabe,
            f"der Kaefig laeuft weiter in den Weiterleiter: {ausgabe[:160]!r}")
    require(GB.resolve() in ausgabe,
            f"im Kaefig wurde eine andere Binary gefunden: {ausgabe[:160]!r}")


def t_p02_the_child_path_prefers_the_trusted_git() -> None:
    """Vorne, nicht hinten. Hinten gewinnt `/usr/bin` — und das ist der
    Weiterleiter."""
    from solvio.specialists import launcher as L

    umgebung = L.child_environment()
    gefunden = shutil.which("git", path=umgebung["PATH"])
    require(gefunden, "im Suchpfad des Kindes gibt es kein git")
    require(not GB.is_forwarder(gefunden),
            f"der Suchpfad des Kindes findet den Weiterleiter: {gefunden}")
    require_equal(gefunden, GB.resolve(),
                  "der Suchpfad findet eine andere Binary als die gepruefte")


def t_p02_no_module_calls_git_by_name_any_more() -> None:
    """Der eigentliche Rueckfallschutz.

    Ein einziger uebersehener Aufruf mit dem blossen Namen `git` bringt den
    Dialog zurueck — und zwar erst nachts, unbeaufsichtigt. Deshalb wird das
    hier gemessen und nicht behauptet.
    """
    erlaubt = os.path.join(REPO, "src", "solvio", "git_binary.py")
    treffer = []
    for basis, ordner, dateien in os.walk(os.path.join(REPO, "src")):
        ordner[:] = [d for d in ordner if d != "__pycache__"]
        for name in dateien:
            if not name.endswith(".py"):
                continue
            pfad = os.path.join(basis, name)
            if pfad == erlaubt:
                continue
            text = open(pfad, encoding="utf-8").read()
            for nr, zeile in enumerate(text.splitlines(), 1):
                nackt = zeile.strip()
                if nackt.startswith("#") or nackt.startswith("#:"):
                    continue
                if '["git"' in zeile or "['git'" in zeile \
                        or '"/usr/bin/git"' in zeile or "'/usr/bin/git'" in zeile:
                    treffer.append(f"{os.path.relpath(pfad, REPO)}:{nr}")
    require_equal(treffer, [],
                  f"git wird noch ueber den Namen oder den Weiterleiter "
                  f"gerufen: {treffer}")


def t_p02_an_explicit_override_is_checked_like_any_other() -> None:
    """Eine Angabe ist eine Auswahl, keine Erlaubnis."""
    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    kein_git = os.path.join(ordner, "git")
    with open(kein_git, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho 'ich bin nicht git'\n")
    os.chmod(kein_git, 0o755)

    grund = GB.why_not(kein_git)
    require(grund.startswith("not_git"),
            f"etwas, das nicht git ist, kam durch: {grund!r}")

    # Und die Aufloesung nimmt es nicht, sondern geht weiter.
    gewaehlt = GB.resolve(env={GB.ENV_OVERRIDE: kein_git,
                               "PATH": "/usr/bin:/bin"}, refresh=True)
    require(gewaehlt != kein_git,
            "eine ungepruefte Angabe wurde uebernommen")
    GB.resolve(refresh=True)   # den Zustand fuer die uebrigen Faelle zuruecksetzen

    # Auch weltschreibbar ist ein Grund, nicht nur „laeuft nicht".
    os.chmod(kein_git, 0o777)
    require_equal(GB.why_not(kein_git), "writable_by_others",
                  "eine weltschreibbare Binary galt als vertrauenswuerdig")


def t_p02_there_is_no_trusted_git_is_an_error_not_a_dialog() -> None:
    """Findet sich nichts, endet es laut — nicht wartend."""
    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    try:
        GB.resolve(env={GB.ENV_OVERRIDE: os.path.join(ordner, "fehlt"),
                        "PATH": ordner}, refresh=True)
    except GB.GitBinaryError as exc:
        require("no_trusted_git" in str(exc), f"falscher Grund: {exc}")
    else:
        # Auf diesem Rechner gewinnt einer der festen Pfade — das ist die
        # gute Lage. Dann muss er wenigstens kein Weiterleiter sein.
        require(not GB.is_forwarder(GB.resolve()), "der Weiterleiter gewann")
    finally:
        GB.resolve(refresh=True)


# =====================================================================
# P0.3 — der bevorzugte Schreiber war strukturell unerreichbar
# =====================================================================

def t_p03_the_canonical_broker_can_lend_the_anthropic_credential() -> None:
    """Der dritte Blocker, in diesem Milestone gefunden statt gesucht.

    Der Autopilot-Treiber holt Token und Lease bewusst aus dem LAUFENDEN Core
    — „zwei Kappen sind keine Kappe". Dessen Broker bekam aber nie einen
    Tresor. Damit war `AnthropicUpstream._vault` `None`, und die zweite
    Flaeche antwortete `no_credential`, egal was im Tresor lag.

    Gemessen am 2026-09-03 im Brokerbuch: `autopilot-writer-claude`, 22
    Aufrufe, 0 Erfolge, alle `no_credential` — seit es diesen Auftraggeber
    gibt. Der bevorzugte Schreiber war nicht knapp, er war unerreichbar.

    Zwei Haelften, weil eine allein nichts beweist: die Flaeche haengt
    wirklich am Tresor, UND der Core bindet ihn wirklich.
    """
    from solvio.provider_broker import BrokerService
    from solvio.secret_vault.broker import SecretBroker

    ohne = BrokerService(provider_key="synthetisch-egal")
    require(not ohne.anthropic.configured(),
            "ohne Tresor gibt sich die Anthropic-Flaeche als konfiguriert aus")

    tresor = SecretBroker()
    mit = BrokerService(provider_key="synthetisch-egal", vault=tresor)
    from solvio.provider_broker import anthropic as AN
    require_equal(mit.anthropic.configured(), bool(tresor.exists(AN.SECRET_REF)),
                  "die Flaeche folgt nicht dem Tresor")

    # Und der Core bindet ihn — sonst waere die Haelfte oben nur eine
    # Moeglichkeit, die niemand nutzt.
    quelle = open(os.path.join(REPO, "src", "solvio", "realtime",
                               "core_server.py"), encoding="utf-8").read()
    stelle = quelle.index("BrokerService(")
    ausschnitt = quelle[stelle:stelle + 120]
    require("vault=" in ausschnitt,
            f"der Core baut den Broker ohne Tresor: {ausschnitt[:80]!r}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
