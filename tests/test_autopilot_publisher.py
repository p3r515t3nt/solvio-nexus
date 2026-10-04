"""Autopilot — die Checkpoint-Publishing-Naht und ihre Autoritaetsgrenze.

Diese Naht ist die einzige Stelle, an der Autopilot-Arbeit den kanonischen
Objektspeicher erreicht. Sie existiert wegen DEBT-0179: der Arbeitsbereich ist
ein isolierter Klon, und das Offsite-Herkunftstor (§8.1) verlangt den
ausfuehrenden Commit im Buendel aus `inventory.CORE_REPO`. Statt das Tor
aufzuweichen, wird der Commit erreichbar gemacht.

Der Builder bekommt dabei **kein** Schreibrecht auf das kanonische Repo. Was
hier geprueft wird, ist genau das: dass diese Naht eng ist und bleibt.

Alle Faelle laufen gegen ECHTE git-Repositorien. Eine Attrappe wuerde meine
Erwartung an git pruefen, nicht git.

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

_SANDBOX = tempfile.mkdtemp(prefix="solvio-autopilot-pub-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.autopilot import publisher as P   # noqa: E402

_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
        "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"}


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", repo, *args], env={**os.environ, **_ENV},
                          capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args[0]}: {proc.stderr[:200]}")
    return proc


def _welt(*, mit_geheimnis: bool = False):
    """Ein kanonisches Repo mit Zweigen und Etiketten, plus ein echter Klon."""
    wurzel = tempfile.mkdtemp(dir=_SANDBOX)
    kanonisch = os.path.join(wurzel, "kanonisch")
    subprocess.run(["git", "init", "-q", "-b", "main", kanonisch],
                   env={**os.environ, **_ENV}, capture_output=True)
    with open(os.path.join(kanonisch, "a.txt"), "w") as fh:
        fh.write("eins\n")
    _git(kanonisch, "add", "-A")
    _git(kanonisch, "commit", "-qm", "erst")
    _git(kanonisch, "tag", "v1")
    _git(kanonisch, "branch", "deploy/compose")

    klon = os.path.join(wurzel, "klon")
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", kanonisch, klon],
                   env={**os.environ, **_ENV}, capture_output=True)
    _git(klon, "checkout", "-q", "-b", "arbeit")
    with open(os.path.join(klon, "b.txt"), "w") as fh:
        fh.write("zwei\n")
    if mit_geheimnis:
        with open(os.path.join(klon, "auth.json"), "w") as fh:
            fh.write('{"tokens": {"access_token": "geheim"}}\n')
    _git(klon, "add", "-A")
    _git(klon, "commit", "-qm", "arbeit")
    commit = _git(klon, "rev-parse", "HEAD").stdout.strip()
    return kanonisch, klon, commit


# ------------------------------------------------------- 1. der erlaubte Weg
def t_a_checkpoint_reaches_the_canonical_store_and_nothing_else_moves() -> None:
    """Der Normalfall — und die Gegenprobe, dass sonst nichts geschah."""
    kanonisch, klon, commit = _welt()
    vorher = P.snapshot(kanonisch)
    pub = P.CheckpointPublisher(kanonisch)

    require(not pub.reachable(commit), "der Commit war vorher schon da")
    ergebnis = pub.publish(clone=klon, commit=commit,
                           milestone_id="probe-milestone",
                           checkpoint_id="cp-eins")

    require_equal(ergebnis.canonical_ref,
                  "refs/autopilot/probe-milestone/cp-eins", "falscher Ref")
    require_equal(ergebnis.commit, commit, "falscher Commit")
    require(pub.reachable(commit), "der Commit ist nicht erreichbar")

    nachher = P.snapshot(kanonisch)
    require_equal(nachher["head"], vorher["head"], "HEAD hat sich bewegt")
    require_equal(nachher["status"], vorher["status"],
                  "der Arbeitsbaum hat sich veraendert")
    neu = set(nachher["refs"]) - set(vorher["refs"])
    require_equal(sorted(neu), ["refs/autopilot/probe-milestone/cp-eins"],
                  f"es entstanden andere Refs: {sorted(neu)}")
    for name, wert in vorher["refs"].items():
        require_equal(nachher["refs"].get(name), wert,
                      f"{name} wurde veraendert")


def t_the_published_commit_is_reachable_for_a_bundle() -> None:
    """Der eigentliche Zweck: das Herkunftstor buendelt mit `--all`.

    Ohne diese Zusicherung waere die ganze Naht eine Behauptung — sie soll ja
    genau das leisten, woran DEBT-0179 haengt.
    """
    kanonisch, klon, commit = _welt()
    P.CheckpointPublisher(kanonisch).publish(
        clone=klon, commit=commit, milestone_id="probe-milestone",
        checkpoint_id="cp-buendel")

    buendel = os.path.join(_SANDBOX, "probe.bundle")
    _git(kanonisch, "bundle", "create", buendel, "--all")
    leer = tempfile.mkdtemp(dir=_SANDBOX)
    subprocess.run(["git", "init", "-q", "--bare", leer],
                   env={**os.environ, **_ENV}, capture_output=True)
    kopf = subprocess.run(["git", "-C", leer, "bundle", "list-heads", buendel],
                          capture_output=True, text=True, check=False).stdout
    require(commit in kopf,
            "der veroeffentlichte Commit steckt nicht im Buendel")
    require("refs/autopilot/probe-milestone/cp-buendel" in kopf,
            f"der Autopilot-Ref fehlt im Buendel: {kopf[:200]}")


# --------------------------------------- 2-5. die verbotenen Ziele
def t_main_deploy_tags_and_head_are_unconstructible() -> None:
    """Vier verbotene Ziele, alle als Bestandteil versucht.

    Der Refname wird nie entgegengenommen, sondern konstruiert — deshalb ist
    der Angriff hier der einzige moegliche: einen Bestandteil einschleusen.
    """
    faelle = (
        ("main als Zweig", "refs/heads", "main"),
        ("deploy-Zweig", "refs/heads", "deploy"),
        ("Etikett", "refs/tags", "v1"),
        ("HEAD", "..", "HEAD"),
        ("Pfadflucht nach oben", "../../..", "main"),
        ("Schraegstrich im Teil", "probe/heads", "cp-1"),
        ("leerer Teil", "", "cp-1"),
    )
    for was, milestone, checkpoint in faelle:
        exc = require_raises(P.PublishRefused, P.build_ref, milestone, checkpoint)
        require_equal(exc.reason, "bad_ref_component",
                      f"{was}: falscher Ablehnungsgrund ({exc.reason})")


def t_the_second_bolt_catches_a_forbidden_ref_even_if_the_pattern_were_loosened() -> None:
    """Verteidigung in der Tiefe: die Sperrliste prueft den FERTIGEN Namen.

    Sie greift auch dann, wenn jemand spaeter das Muster lockert — genau
    dafuer ist sie da.
    """
    # Jeder Riegel mit dem Fall, den NUR er faengt. Eine Mutation hat gezeigt,
    # dass eine Sperrliste hinter der Namensraum-Pruefung unerreichbar ist —
    # sie sah aus wie Sicherheit und war Verzierung.
    genau = (
        ("refs/heads/main", "forbidden_ref_prefix"),
        ("refs/heads/deploy/compose", "forbidden_ref_prefix"),
        ("refs/tags/v1", "forbidden_ref_prefix"),
        ("refs/remotes/origin/main", "forbidden_ref_prefix"),
        ("HEAD", "forbidden_ref"),
        ("FETCH_HEAD", "forbidden_ref"),
        ("refs/etwas/anderes/x", "ref_outside_namespace"),
        ("refs/autopilot/../heads/x", "malformed_ref"),
        ("refs/autopilot/m/cp/zuviel", "malformed_ref"),
        ("refs/autopilot/m/", "malformed_ref"),
    )
    for ref, grund in genau:
        exc = require_raises(P.PublishRefused, P.assert_safe_ref, ref)
        require_equal(exc.reason, grund,
                      f"{ref}: erwartet {grund}, bekam {exc.reason}")
    P.assert_safe_ref("refs/autopilot/m/cp-1")     # die Gegenprobe


def t_the_publisher_cannot_run_a_forbidden_git_subcommand() -> None:
    """`push`, `checkout`, `reset` und Freunde sind strukturell unmoeglich.

    Die Pruefung sitzt in `_git` und nicht bei den Aufrufern: eine Grenze, die
    jeder Aufrufer selbst einhalten muss, ist keine.
    """
    kanonisch, _klon, _commit = _welt()
    for unterbefehl in ("push", "checkout", "switch", "reset", "tag", "branch",
                        "merge", "gc", "filter-branch", "remote"):
        exc = require_raises(P.PublishRefused, P._git, kanonisch, unterbefehl)
        require_equal(exc.reason, "forbidden_subcommand",
                      f"{unterbefehl} wurde nicht abgewiesen")
    require("push" not in P.ALLOWED_SUBCOMMANDS, "push steht auf der Liste")
    for arg in ("--force", "--mirror", "--delete"):
        exc = require_raises(P.PublishRefused, P._git, kanonisch, "fetch", arg)
        require_equal(exc.reason, "forbidden_argument", f"{arg} ging durch")
    exc = require_raises(P.PublishRefused, P._git, kanonisch, "fetch",
                         "+refs/heads/*:refs/heads/*")
    require_equal(exc.reason, "forced_refspec", "ein erzwungener Refspec ging durch")


# ------------------------------------------------- 6. Unveraenderlichkeit
def t_a_published_checkpoint_cannot_be_bent_to_another_commit() -> None:
    """Ein veroeffentlichter Checkpoint bleibt, worauf er zeigt.

    Sonst waere die Audit-Wahrheit rueckwirkend veraenderbar — und ein
    Herkunftsbeweis, der sich umschreiben laesst, beweist nichts.
    """
    kanonisch, klon, erster = _welt()
    pub = P.CheckpointPublisher(kanonisch)
    pub.publish(clone=klon, commit=erster, milestone_id="probe-milestone",
                checkpoint_id="cp-fest")

    with open(os.path.join(klon, "c.txt"), "w") as fh:
        fh.write("drei\n")
    _git(klon, "add", "-A")
    _git(klon, "commit", "-qm", "spaeter")
    zweiter = _git(klon, "rev-parse", "HEAD").stdout.strip()

    exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                         commit=zweiter, milestone_id="probe-milestone",
                         checkpoint_id="cp-fest")
    require_equal(exc.reason, "checkpoint_ref_exists", f"falscher Grund: {exc.reason}")
    require_equal(P.snapshot(kanonisch)["refs"][
        "refs/autopilot/probe-milestone/cp-fest"], erster,
        "der Ref wurde trotzdem umgebogen")

    # Ein NEUER Checkpoint ist der vorgesehene Weg.
    zweite = pub.publish(clone=klon, commit=zweiter,
                         milestone_id="probe-milestone", checkpoint_id="cp-neu")
    require_equal(zweite.commit, zweiter, "der neue Checkpoint stimmt nicht")


# ------------------------------------------------- 7. Kredentialfund
def t_a_credential_finding_leaves_no_ref_at_all() -> None:
    """Fail closed heisst: es entsteht NICHTS. Nicht „es entsteht und wir melden"."""
    kanonisch, klon, commit = _welt(mit_geheimnis=True)
    vorher = P.snapshot(kanonisch)
    pub = P.CheckpointPublisher(kanonisch)

    exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                         commit=commit, milestone_id="probe-milestone",
                         checkpoint_id="cp-schmuggel")
    require_equal(exc.reason, "credential_in_checkpoint", f"falscher Grund: {exc.reason}")
    require("auth.json" in exc.detail, f"die Datei wird nicht benannt: {exc.detail}")

    nachher = P.snapshot(kanonisch)
    require_equal(sorted(set(nachher["refs"]) - set(vorher["refs"])), [],
                  "es entstand trotz Kredentialfund ein Ref")
    require(not pub.reachable(commit),
            "die Objekte wurden trotz Kredentialfund uebertragen")


# ------------------------------------------- 8. der Commit muss der richtige sein
def t_only_the_commit_that_will_be_gated_is_published() -> None:
    """Was veroeffentlicht wird, muss der `result_commit` sein — sonst pruefte
    das Gate einen anderen Baum als den erreichbar gemachten."""
    kanonisch, klon, erster = _welt()
    with open(os.path.join(klon, "c.txt"), "w") as fh:
        fh.write("drei\n")
    _git(klon, "add", "-A")
    _git(klon, "commit", "-qm", "spaeter")
    pub = P.CheckpointPublisher(kanonisch)

    exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                         commit=erster, milestone_id="probe-milestone",
                         checkpoint_id="cp-alt")
    require_equal(exc.reason, "commit_is_not_head",
                  f"ein alter Commit wurde veroeffentlicht: {exc.reason}")


def t_a_dirty_clone_is_not_publishable() -> None:
    """Ein unnormalisierter Arbeitsbaum heisst: der Commit ist nicht die Arbeit."""
    kanonisch, klon, commit = _welt()
    with open(os.path.join(klon, "unfertig.txt"), "w") as fh:
        fh.write("x")
    pub = P.CheckpointPublisher(kanonisch)
    exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                         commit=commit, milestone_id="probe-milestone",
                         checkpoint_id="cp-schmutzig")
    require_equal(exc.reason, "clone_not_normalised", f"falscher Grund: {exc.reason}")


def t_a_contract_drift_stops_the_publication() -> None:
    """Der gepinnte Contract gilt auch hier."""
    kanonisch, klon, commit = _welt()
    pub = P.CheckpointPublisher(kanonisch)
    exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                         commit=commit, milestone_id="probe-milestone",
                         checkpoint_id="cp-drift",
                         contract_hash="sha256:aaa",
                         expected_contract_hash="sha256:bbb")
    require_equal(exc.reason, "contract_drift", f"falscher Grund: {exc.reason}")


# --------------------------------- 9./10. Produktionsbaum und origin
def t_the_canonical_worktree_and_origin_are_untouched() -> None:
    """Kein Checkout, kein Reset, kein Push — gemessen, nicht zugesichert."""
    kanonisch, klon, commit = _welt()
    # Ein „origin" fuer den kanonischen Baum, damit ein Push sichtbar waere.
    fern = tempfile.mkdtemp(dir=_SANDBOX)
    subprocess.run(["git", "init", "-q", "--bare", fern],
                   env={**os.environ, **_ENV}, capture_output=True)
    _git(kanonisch, "remote", "add", "origin", fern)
    _git(kanonisch, "push", "-q", "origin", "main")
    fern_vorher = subprocess.run(["git", "-C", fern, "for-each-ref"],
                                 capture_output=True, text=True).stdout
    dateien_vorher = sorted(os.listdir(kanonisch))

    ergebnis = P.CheckpointPublisher(kanonisch).publish(
        clone=klon, commit=commit, milestone_id="probe-milestone",
        checkpoint_id="cp-fern")

    require(ergebnis.untouched["head_unchanged"], "HEAD hat sich bewegt")
    require(ergebnis.untouched["worktree_unchanged"], "der Arbeitsbaum aenderte sich")
    require(ergebnis.untouched["only_expected_ref"],
            f"unerwartete Refbewegung: {ergebnis.untouched}")
    require_equal(sorted(os.listdir(kanonisch)), dateien_vorher,
                  "im Arbeitsbaum kamen Dateien hinzu oder verschwanden")
    fern_nachher = subprocess.run(["git", "-C", fern, "for-each-ref"],
                                  capture_output=True, text=True).stdout
    require_equal(fern_nachher, fern_vorher,
                  "origin hat sich veraendert — es wurde gepusht")


def t_an_unexpected_ref_movement_aborts_the_publication() -> None:
    """Die Nachpruefung ist eine Sperre, nicht ein Bericht.

    Eine Mutation hat gezeigt, dass sie sich entfernen liess, ohne dass etwas
    rot wurde: die Suite las das ERGEBNIS der Pruefung statt ihre Wirkung.
    Hier bewegt sich waehrend der Publikation ein fremder Ref — und dann darf
    die Publikation nicht gelingen.
    """
    kanonisch, klon, commit = _welt()
    pub = P.CheckpointPublisher(kanonisch)
    echt = P.snapshot
    zustand = {"aufrufe": 0}

    def _mit_fremder_bewegung(repo):
        ergebnis = echt(repo)
        zustand["aufrufe"] += 1
        if zustand["aufrufe"] == 1:
            # Der Schnappschuss VORHER behauptet einen Ref, den es danach
            # nicht mehr gibt — als haette jemand daneben etwas bewegt.
            ergebnis = dict(ergebnis)
            ergebnis["refs"] = dict(ergebnis["refs"])
            ergebnis["refs"]["refs/heads/verschwindet"] = "0" * 40
        return ergebnis

    P.snapshot = _mit_fremder_bewegung
    try:
        exc = require_raises(P.PublishRefused, pub.publish, clone=klon,
                             commit=commit, milestone_id="probe-milestone",
                             checkpoint_id="cp-bewegung")
    finally:
        P.snapshot = echt
    require_equal(exc.reason, "unexpected_ref_change",
                  f"eine fremde Refbewegung ging durch: {exc.reason}")


def t_the_publisher_refuses_to_treat_the_canonical_repo_as_its_own_clone() -> None:
    """Ein „Klon", der der kanonische Baum IST, waere kein Klon."""
    kanonisch, _klon, _commit = _welt()
    commit = _git(kanonisch, "rev-parse", "HEAD").stdout.strip()
    pub = P.CheckpointPublisher(kanonisch)
    exc = require_raises(P.PublishRefused, pub.publish, clone=kanonisch,
                         commit=commit, milestone_id="probe-milestone",
                         checkpoint_id="cp-selbst")
    require_equal(exc.reason, "clone_is_canonical", f"falscher Grund: {exc.reason}")


def t_checkpoint_ids_are_core_generated() -> None:
    """Ein Modell schlaegt keine Checkpoint-Kennung vor."""
    eins = P.new_checkpoint_id(now=1_800_000_000.0)
    zwei = P.new_checkpoint_id(now=1_800_000_000.0)
    require(eins != zwei, "zwei Kennungen zur selben Zeit waren gleich")
    for kennung in (eins, zwei):
        P.build_ref("probe-milestone", kennung)       # muss durchs Muster passen


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
