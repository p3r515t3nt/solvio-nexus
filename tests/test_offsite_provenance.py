"""Offsite V1 — die Herkunft des Buendels, und warum sie bewiesen sein muss.

Diese Suite existiert wegen eines einzigen, teuer bezahlten Befundes: die
Generation `20260901T054003Z` bestand `git bundle verify`, trug 139 Referenzen
und war trotzdem im Ernstfall wertlos. `git clone` checkt die **HEAD des
Buendels** aus, und die kam aus einem fremden, losgeloesten Baum ohne
`offsite`-Paket. Das Restore-Werkzeug lag im Buendel — nur nicht dort, wo ein
Klon es findet.

Was hier geprueft wird, ist deshalb nie „ist das Buendel gueltig". Es ist:
**laesst sich aus diesem Buendel allein das Werkzeug materialisieren, mit dem
man den Satz zurueckholt?** Jeder Fall unten baut echte git-Repositories und
echte Buendel — es gibt hier nichts nachzubilden.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import importlib.util
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

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-prov-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.storage.offsite import provenance as OPV          # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# --------------------------------------------------------------------- Werkzeug
def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Pruefung", "GIT_AUTHOR_EMAIL": "p@example",
                "GIT_COMMITTER_NAME": "Pruefung",
                "GIT_COMMITTER_EMAIL": "p@example"})
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, text=True,
                          capture_output=True, check=False, timeout=120)
    require(proc.returncode == 0,
            f"git {' '.join(args[:3])} scheiterte: {proc.stderr.strip()[:160]}")
    return proc


def _baum(name: str, *, weglassen: tuple[str, ...] = ()) -> str:
    """Ein echtes Repository mit der Werkzeugkette — oder mit Luecken darin."""
    root = tempfile.mkdtemp(prefix=f"{name}-", dir=_SANDBOX)
    _git(["init", "--quiet", "-b", "haupt", root])
    for rel in OPV.REQUIRED_PATHS:
        if rel in weglassen:
            continue
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# {rel} aus {name}\n")
    # Ein Repository ohne jede Datei laesst sich nicht committen.
    with open(os.path.join(root, "MARKE"), "w", encoding="utf-8") as fh:
        fh.write(name + "\n")
    _git(["-C", root, "add", "-A"])
    _git(["-C", root, "commit", "--quiet", "-m", f"Stand {name}"])
    return root


def _buendel(repo: str, ziel_name: str = "core.bundle") -> str:
    """Ein Satz-Skelett mit `Repos/<name>` — genau wie die Engine ihn legt."""
    satz = tempfile.mkdtemp(prefix="satz-", dir=_SANDBOX)
    os.makedirs(os.path.join(satz, "Repos"), exist_ok=True)
    dest = os.path.join(satz, "Repos", ziel_name)
    _git(["-C", repo, "bundle", "create", dest, "--all"])
    return satz


class _AlsErzeuger:
    """Laesst `provenance` einen bestimmten Baum fuer den ausfuehrenden halten.

    Kein Text-Patch: `producer_root()` ist die einzige Stelle, an der das
    Modul erfaehrt, wer laeuft — genau die wird hier gestellt.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self._alt = None

    def __enter__(self) -> str:
        self._alt = OPV.producer_root
        OPV.producer_root = lambda: self.root
        return _git(["-C", self.root, "rev-parse", "HEAD"]).stdout.strip()

    def __exit__(self, *exc) -> None:
        OPV.producer_root = self._alt


def _scheitert(fn, grund: str, was: str) -> None:
    try:
        fn()
    except OPV.ProvenanceError as exc:
        require_equal(exc.reason, grund, f"{was}: falscher Grund")
        return
    raise AssertionError(f"{was}: der Beweis ging durch, obwohl er nicht durfte")


# --------------------------------------------------------- der Beweis traegt
def t_der_ausfuehrende_baum_wird_im_buendel_wiedergefunden() -> None:
    """Der Normalfall — und er sagt mehr als „gueltig".

    Bewiesen wird, dass der Commit, der laeuft, im Buendel steckt und dass
    sich aus ihm das Werkzeug im isolierten Klon materialisiert.
    """
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    with _AlsErzeuger(repo) as commit:
        proof = OPV.prove_staging_set(satz, scratch_root=_SANDBOX)
    require_equal(proof.producer_commit, commit,
                  "der Beweis nennt einen anderen Erzeuger als den laufenden")
    require_equal(proof.core_bundle_commit, commit,
                  "Erzeuger und Buendelstand fallen auseinander")
    require_equal(proof.core_bundle_ref, "refs/heads/haupt",
                  "die Referenz zum Auschecken fehlt")
    require(proof.bundle_refs >= 1, "das Buendel traegt keine Referenzen")
    require_equal(len(proof.checked_paths), len(OPV.REQUIRED_PATHS),
                  "es wurde nicht die ganze Werkzeugkette geprueft")


def t_die_bewiesene_herkunft_reist_in_der_generation_mit() -> None:
    """`as_dict()` ist das, was spaeter in `offsite.json` steht.

    Ohne diese Felder kann ein Klon im Ernstfall nicht wissen, welchen Commit
    er auschecken muss — und faellt wieder auf die HEAD des Buendels zurueck.
    """
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    with _AlsErzeuger(repo) as commit:
        payload = OPV.prove_staging_set(satz, scratch_root=_SANDBOX).as_dict()
    require_equal(payload["producer_commit"], commit, "producer_commit fehlt")
    require_equal(payload["core_bundle_commit"], commit,
                  "core_bundle_commit fehlt")
    require_equal(payload["restore_entry"], OPV.ENTRY_PATH,
                  "der Einstiegspunkt ist nicht benannt")
    require(len(payload["restore_entry_sha256"]) == 64,
            "die Pruefsumme des Werkzeugs fehlt")


def t_der_erzeuger_kommt_aus_der_datei_nicht_aus_einer_konstante() -> None:
    """Genau diese Verwechslung war der Defekt.

    `inventory.CORE_REPO` beschreibt einen Baum; `__file__` beschreibt den
    laufenden. Nur der zweite kann nicht luegen — und `provenance` muss den
    zweiten benutzen.
    """
    from solvio.storage import inventory
    root = OPV.producer_root()
    require_equal(os.path.realpath(root), os.path.realpath(REPO_ROOT),
                  "producer_root zeigt nicht auf den ausfuehrenden Baum")
    hier = os.path.realpath(inventory.CORE_REPO)
    if hier != os.path.realpath(REPO_ROOT):
        require(os.path.realpath(root) != hier,
                "producer_root folgt CORE_REPO statt der laufenden Datei")


# ------------------------------------------------------- und er faellt richtig
def t_ein_buendel_ohne_werkzeug_ist_kein_beweis() -> None:
    """Der Kernbefund: gueltig, klonbar — und trotzdem wertlos."""
    repo = _baum("ohne-werkzeug", weglassen=(OPV.ENTRY_PATH,))
    satz = _buendel(repo)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "entry_missing", "Buendel ohne restore.py")


def t_ein_buendel_mit_luecke_in_der_kette_ist_kein_beweis() -> None:
    """`restore.py` allein genuegt nicht — es importiert beim Laden.

    Ein Buendel, das den Einstieg traegt, aber `pack.py` nicht, faellt sonst
    erst im Ernstfall auf: dann, wenn niemand mehr nachbessern kann.
    """
    fehlt = "src/solvio/storage/offsite/pack.py"
    require(fehlt in OPV.REQUIRED_PATHS, "die Kette prueft pack.py nicht")
    repo = _baum("luecke", weglassen=(fehlt,))
    satz = _buendel(repo)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "entry_missing", "Buendel ohne pack.py")


def t_ein_buendel_aus_dem_falschen_baum_faellt_durch() -> None:
    """Die Mutation „falsche Repo-Wurzel fuer core.bundle".

    Beide Baeume sind fuer sich gueltig und tragen das Werkzeug. Trotzdem ist
    ein Buendel des einen kein Backup des Laufs im anderen.
    """
    erzeuger = _baum("laeuft")
    fremd = _baum("fremd")
    satz = _buendel(fremd)
    with _AlsErzeuger(erzeuger):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "commit_absent", "Buendel aus einem fremden Baum")


def t_ein_erwarteter_commit_der_nicht_laeuft_faellt_durch() -> None:
    """producer_commit != bundle_commit muss rot sein, nicht bloss auffaellig."""
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    bundle = os.path.join(satz, OPV.BUNDLE_RELPATH)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_bundle(bundle, expected_commit="0" * 40,
                                            scratch_root=_SANDBOX),
                   "producer_mismatch", "fremder erwarteter Commit")


def t_ein_buendel_mit_zerstoertem_kopf_faellt_beim_verify_durch() -> None:
    """Der Kopf traegt die Referenzen — ohne ihn ist es kein Buendel."""
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    bundle = os.path.join(satz, OPV.BUNDLE_RELPATH)
    with open(bundle, "r+b") as fh:
        fh.write(b"# v2 git kaputt\n" + b"\x00" * 40)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "bundle_invalid", "Buendel mit zerstoertem Kopf")


def t_ein_buendel_mit_zerstoertem_pack_faellt_beim_klonen_durch() -> None:
    """Gemessen, nicht angenommen: `git bundle verify` prueft den Packinhalt NICHT.

    Ein Buendel, dessen Kopf stimmt und dessen Pack zerstoert ist, besteht
    `verify` anstandslos. Erst der Klon merkt es. Genau deshalb ist der Klon
    Teil des Beweises und nicht bloss Bequemlichkeit — wer ihn wegliesse,
    haette eine Pruefung, die kaputte Buendel durchwinkt.
    """
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    bundle = os.path.join(satz, OPV.BUNDLE_RELPATH)
    with open(bundle, "r+b") as fh:      # Kopf intakt lassen, Pack zerstoeren
        fh.seek(200)
        fh.write(b"\x00" * 4096)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "clone_failed", "Buendel mit zerstoertem Pack")


def t_ein_fehlendes_buendel_faellt_durch() -> None:
    """Ein Satz ohne core.bundle traegt keinen Code — er ist kein Backup."""
    satz = tempfile.mkdtemp(prefix="leer-", dir=_SANDBOX)
    os.makedirs(os.path.join(satz, "Repos"), exist_ok=True)
    repo = _baum("erzeuger")
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=_SANDBOX),
                   "bundle_missing", "Satz ohne core.bundle")


def t_ein_klon_im_laufenden_baum_ist_kein_isolierter_klon() -> None:
    """Ein Klon INNERHALB des Erzeugers beweist nichts ueber den Ernstfall."""
    repo = _baum("erzeuger")
    satz = _buendel(repo)
    with _AlsErzeuger(repo):
        _scheitert(lambda: OPV.prove_staging_set(satz, scratch_root=repo),
                   "clone_not_isolated", "Klon im ausfuehrenden Baum")


# ------------------------------------------- der Riegel des Katastrophenbeweises
def _disaster_modul():
    """Der echte §23.10-Beweis, als Modul geladen — keine Nachbildung."""
    path = os.path.join(REPO_ROOT, "scripts", "offsite_disaster_restore.py")
    require(os.path.isfile(path), "der Katastrophenbeweis fehlt")
    spec = importlib.util.spec_from_file_location("b6_disaster_proof", path)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _lauf(code: str, cwd: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SOLVIO_") and k not in ("PYTHONPATH",
                                                        "PYTHONHOME")}
    env["PYTHONNOUSERSITE"] = "1"
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                          text=True, capture_output=True, check=False,
                          timeout=120)


def _falsches_solvio(wo: str) -> str:
    """Ein fremdes `solvio`-Paket, wie es auf dem alten Mac laege."""
    root = tempfile.mkdtemp(prefix=wo + "-", dir=_SANDBOX)
    pkg = os.path.join(root, "src", "solvio", "storage", "offsite")
    os.makedirs(pkg, exist_ok=True)
    for teil in ("solvio", "solvio/storage", "solvio/storage/offsite"):
        with open(os.path.join(root, "src", *teil.split("/"), "__init__.py"),
                  "w", encoding="utf-8") as fh:
            fh.write("")
    with open(os.path.join(pkg, "restore.py"), "w", encoding="utf-8") as fh:
        fh.write("def verify_unpacked(*a, **k):\n"
                 "    raise SystemExit('fremder Code hat gerechnet')\n")
    return root


def t_der_katastrophenbeweis_rechnet_nur_mit_code_aus_dem_klon() -> None:
    """Die Mutation „Rueckfall auf Code dieses Rechners" muss rot werden.

    Gestellt wird der echte Riegel aus `b6_disaster_proof.pruefcode`: ein
    fremdes SOLVIO liegt frueher auf dem Suchpfad. Der Beweis darf damit nicht
    rechnen — er muss mit Exit 3 abbrechen und die Herkunft nennen.
    """
    modul = _disaster_modul()
    fremd = _falsches_solvio("alter-mac")
    klon = os.path.join(_SANDBOX, "klon-ohne-werkzeug")
    os.makedirs(os.path.join(klon, "src"), exist_ok=True)

    code = modul.pruefcode(klon, os.path.join(_SANDBOX, "satz"), "20260101T000000Z")
    # Der fremde Baum gewinnt den Suchpfad — genau die verbotene Lage.
    code = f"import sys\nsys.path.insert(0, {os.path.join(fremd, 'src')!r})\n" + code
    proc = _lauf(code, _SANDBOX)
    require_equal(proc.returncode, 3,
                  f"der Beweis rechnete mit fremdem Code weiter "
                  f"(exit {proc.returncode}: {proc.stderr.strip()[-160:]})")
    bericht = json.loads(proc.stdout.strip().splitlines()[-1])
    require(bericht["ok"] is False, "ein fremder Lauf meldete Erfolg")
    require(bericht["werkzeug_herkunft"].startswith(fremd),
            "die gemeldete Herkunft benennt den fremden Baum nicht")


def t_der_katastrophenbeweis_kennt_keinen_ersatzbaum_mehr() -> None:
    """Ohne Werkzeug im Klon endet die Kette — sie weicht nicht aus.

    Geprueft am Verhalten des Riegels: zeigt der Suchpfad auf einen Klon ohne
    `restore.py` und liegt sonst nichts bereit, gibt es keinen zweiten Weg.
    Der Lauf scheitert, statt still den Baum dieses Rechners zu nehmen.
    """
    modul = _disaster_modul()
    klon = os.path.join(_SANDBOX, "klon-leer")
    os.makedirs(os.path.join(klon, "src"), exist_ok=True)
    code = modul.pruefcode(klon, os.path.join(_SANDBOX, "satz"), "20260101T000000Z")
    proc = _lauf(code, REPO_ROOT)
    require(proc.returncode != 0,
            "ein Klon ohne Werkzeug lieferte trotzdem ein Ergebnis")
    require("ModuleNotFoundError" in proc.stderr or proc.returncode == 3,
            f"unerwarteter Ausgang: {proc.stderr.strip()[-200:]}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
