"""Die Herkunft des Buendels — bewiesen, BEVOR etwas das Haus verlaesst.

Der Befund, der dieses Modul erzwungen hat, ist teuer bezahlt. Generation
`20260901T054003Z` trug ein gueltiges `core.bundle` mit 139 Referenzen, das
`git bundle verify` anstandslos bestand — und trotzdem stand im Klon kein
Restore-Werkzeug. Der Grund:

* `git clone <bundle>` checkt die **HEAD des Buendels** aus.
* Die HEAD kam aus `inventory.CORE_REPO` (dem Produktionsbaum), der auf einem
  losgeloesten Commit `3583d5c` parkte — einem Stand ohne `offsite`-Paket.
* Ausgefuehrt hat den Lauf aber ein **anderer** Baum (der Worktree, Commit
  `816ddde`), und genau dessen Code traegt das Werkzeug.
* `offsite.json` schrieb `core_commit` aus `CORE_REPO` — es beschrieb also
  einen Baum, der den Lauf nie ausgefuehrt hat. Eine Herkunftsangabe, die
  strukturell etwas anderes meint, als sie sagt.

Daraus folgt die Regel, die dieses Modul durchsetzt: **der Commit, der den
Lauf ausfuehrt, muss im Buendel liegen, und aus genau diesem Commit muss sich
das Restore-Werkzeug im isolierten Klon materialisieren lassen.** Nicht „das
Buendel ist gueltig". Nicht „irgendwo im Buendel steckt schon etwas".

Alles hier ist fail-closed: was nicht bewiesen ist, ist gescheitert. Ein
Fehler bedeutet, dass die Generation nie hochgeladen, nie verifiziert und nie
gesund wird.

Die Pruefung laeuft unter Katastrophenbedingungen, nicht unter Laborbedingungen:
`git bundle verify` aus einem **leeren** Repository (im Ernstfall gibt es kein
vorhandenes), der Klon in einem **isolierten** Verzeichnis ausserhalb jedes
Produktionsbaums, ohne Objekt-Alternates, mit neutralisierter git-Konfiguration.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio import git_binary as _GB

log = get_logger(__name__)


#: Der Einstiegspunkt, um den es geht. Fehlt er, ist die Generation ein Archiv
#: ohne Werkzeug — im Totalverlust wertlos.
ENTRY_PATH = "src/solvio/storage/offsite/restore.py"

#: Der Einstieg allein genuegt nicht: er importiert beim Laden. Ein Buendel,
#: das `restore.py` traegt, aber `pack.py` nicht, ist genauso unbrauchbar —
#: nur faellt es erst im Ernstfall auf. Deshalb wird die ganze Kette geprueft,
#: die `restore.py` modulweit importiert.
REQUIRED_PATHS = (
    ENTRY_PATH,
    "src/solvio/storage/offsite/__init__.py",
    "src/solvio/storage/offsite/config.py",
    "src/solvio/storage/offsite/identity.py",
    "src/solvio/storage/offsite/ledger.py",
    "src/solvio/storage/offsite/pack.py",
    "src/solvio/storage/offsite/s3.py",
    "src/solvio/storage/offsite/verify.py",
    "src/solvio/storage/restore.py",
    "src/solvio/storage/inventory.py",
    "src/solvio/logging_setup.py",
)

#: Relativer Ort des Kern-Buendels im Sicherungssatz.
BUNDLE_RELPATH = os.path.join("Repos", "core.bundle")


class ProvenanceError(Exception):
    """Die Herkunft ist nicht bewiesen — und damit ist die Generation tot.

    `reason` ist ein kurzes, geschlossenes Wort. Es steht im Buch, im State und
    in der Meldung, damit ein Fehler genau einen Ort hat.
    """

    def __init__(self, reason: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - triviale Formatierung
        base = f"{self.reason}: {super().__str__()}"
        return f"{base} ({self.detail})" if self.detail else base


@dataclass
class Proof:
    """Was bewiesen wurde — und nur das."""

    producer_commit: str
    producer_branch: str
    producer_dirty: bool
    producer_root: str
    core_bundle_commit: str
    core_bundle_ref: str
    bundle_sha256: str
    bundle_refs: int
    entry_sha256: str
    checked_paths: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "producer_commit": self.producer_commit,
            "producer_branch": self.producer_branch,
            "producer_dirty": self.producer_dirty,
            "core_bundle_commit": self.core_bundle_commit,
            "core_bundle_ref": self.core_bundle_ref,
            "core_bundle_sha256": self.bundle_sha256,
            "core_bundle_refs": self.bundle_refs,
            "restore_entry": ENTRY_PATH,
            "restore_entry_sha256": self.entry_sha256,
            "restore_required_paths": list(self.checked_paths),
        }


# ------------------------------------------------------------------ der Erzeuger
def producer_root() -> str:
    """Der Baum, der DIESEN Code ausfuehrt — abgeleitet aus `__file__`.

    Bewusst **keine** Konstante und bewusst nicht `inventory.CORE_REPO`. Genau
    diese Verwechslung war der Defekt: eine Konstante beschreibt einen Baum,
    `__file__` beschreibt den laufenden. Nur der zweite kann nicht luegen.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # .../<root>/src/solvio/storage/offsite/provenance.py  ->  <root>
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir,
                                        os.pardir, os.pardir))


def _git(args: list[str], *, cwd: str | None = None,
         timeout: float = 300.0) -> subprocess.CompletedProcess:
    """git ohne Umgebungsgedaechtnis.

    Ein geerbtes `GIT_DIR` oder eine globale Konfiguration wuerde den Klon an
    den Rechner binden, auf dem er entsteht — und damit genau die Isolation
    aufweichen, die hier bewiesen werden soll.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "LC_ALL": "C"})
    return subprocess.run([_GB.resolve(), *args], cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False)


def producer_revision() -> tuple[str, str, bool]:
    """`(commit, branch, dirty)` des ausfuehrenden Baums."""
    root = producer_root()
    head = _git(["-C", root, "rev-parse", "HEAD"])
    if head.returncode != 0:
        raise ProvenanceError(
            "producer_unknown",
            "der ausfuehrende Baum ist kein git-Repository",
            detail=f"{root}: {head.stderr.strip()[:160]}")
    commit = head.stdout.strip()
    branch = _git(["-C", root, "rev-parse", "--abbrev-ref",
                   "HEAD"]).stdout.strip() or "HEAD"
    status = _git(["-C", root, "status", "--porcelain"])
    dirty = bool(status.stdout.strip())
    return commit, branch, dirty


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------------------------------- der Beweis
def prove_bundle(bundle_path: str, *, expected_commit: str,
                 scratch_root: str | None = None) -> Proof:
    """Beweist, dass aus `bundle_path` allein restauriert werden kann.

    Sieben Schritte, jeder mit genau einem Grund, an dem er scheitern kann.
    Der Klon lebt in einem Wegwerf-Verzeichnis und wird immer aufgeraeumt.
    """
    if not os.path.isfile(bundle_path):
        raise ProvenanceError("bundle_missing",
                              "der Satz traegt kein core.bundle",
                              detail=bundle_path)
    if os.path.getsize(bundle_path) <= 0:
        raise ProvenanceError("bundle_missing", "core.bundle ist leer",
                              detail=bundle_path)
    bundle_path = os.path.abspath(bundle_path)

    scratch = tempfile.mkdtemp(prefix="offsite-provenance-", dir=scratch_root)
    try:
        return _prove_in(scratch, bundle_path, expected_commit)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _prove_in(scratch: str, bundle_path: str, expected_commit: str) -> Proof:
    commit, branch, dirty = producer_revision()
    root = producer_root()

    # Der Aufrufer sagt, welcher Commit erwartet wird; abweichen darf er nicht.
    # Ein Buendel, das einen ANDEREN Stand traegt als den ausgefuehrten, ist
    # kein Backup dieses Laufs — auch wenn beide fuer sich gueltig sind.
    if expected_commit != commit:
        raise ProvenanceError(
            "producer_mismatch",
            "der erwartete Commit ist nicht der ausfuehrende",
            detail=f"erwartet {expected_commit[:12]}, laeuft {commit[:12]}")

    # -- 1: verify AUS EINEM LEEREN REPOSITORY -----------------------------
    # Im Ernstfall gibt es kein vorhandenes Repository. `git bundle verify`
    # verlangt aber eines — wer die Pruefung im Produktionsbaum laufen laesst,
    # prueft unter Bedingungen, die es dann nicht gibt.
    empty = os.path.join(scratch, "leer")
    os.makedirs(empty, exist_ok=True)
    init = _git(["init", "--quiet", "--bare", empty])
    if init.returncode != 0:
        raise ProvenanceError("scratch_failed",
                              "kein leeres Pruef-Repository moeglich",
                              detail=init.stderr.strip()[:200])
    verified = _git(["-C", empty, "bundle", "verify", bundle_path])
    if verified.returncode != 0:
        raise ProvenanceError("bundle_invalid",
                              "git bundle verify schlug fehl",
                              detail=verified.stderr.strip()[:200])

    heads = _git(["-C", empty, "bundle", "list-heads", bundle_path])
    refs: list[tuple[str, str]] = []
    for line in heads.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            refs.append((parts[0].strip(), parts[1].strip()))

    # -- 2: isoliert klonen ------------------------------------------------
    # Der Klon ist keine Bequemlichkeit, sondern die eigentliche
    # Integritaetspruefung. Gemessen: `git bundle verify` prueft Kopf und
    # Voraussetzungen, aber NICHT den Packinhalt — ein Buendel mit zerstoertem
    # Pack besteht `verify` anstandslos und scheitert erst hier.
    clone = os.path.join(scratch, "klon")
    cloned = _git(["clone", "--quiet", "--no-local", bundle_path, clone])
    if cloned.returncode != 0 or not os.path.isdir(os.path.join(clone, ".git")):
        raise ProvenanceError("clone_failed",
                              "das Buendel laesst sich nicht klonen",
                              detail=cloned.stderr.strip()[:200])

    # Ein Klon mit Alternates borgt Objekte vom Rechner, auf dem er entsteht.
    # Dann bewiese der Klon nichts ueber das Buendel.
    alternates = os.path.join(clone, ".git", "objects", "info", "alternates")
    if os.path.exists(alternates):
        raise ProvenanceError("clone_not_isolated",
                              "der Klon borgt Objekte vom laufenden Rechner",
                              detail=alternates)
    if os.path.abspath(clone).startswith(os.path.abspath(root) + os.sep):
        raise ProvenanceError("clone_not_isolated",
                              "der Klon liegt im ausfuehrenden Baum",
                              detail=clone)

    # -- 3: traegt der Klon den ausfuehrenden Commit? ----------------------
    present = _git(["-C", clone, "cat-file", "-e", f"{commit}^{{commit}}"])
    if present.returncode != 0:
        raise ProvenanceError(
            "commit_absent",
            "der ausfuehrende Commit steckt nicht im Buendel",
            detail=f"{commit[:12]} fehlt in {os.path.basename(bundle_path)}")

    # -- 4: genau diesen Commit auschecken ---------------------------------
    # NICHT die HEAD des Buendels: genau die war der Defekt.
    checked = _git(["-C", clone, "checkout", "--quiet", "--detach", commit])
    if checked.returncode != 0:
        raise ProvenanceError("checkout_failed",
                              "der ausfuehrende Commit laesst sich im Klon "
                              "nicht auschecken",
                              detail=checked.stderr.strip()[:200])

    # -- 5: liegt das Werkzeug wirklich als Datei da? ----------------------
    for rel in REQUIRED_PATHS:
        on_disk = os.path.join(clone, rel)
        if not os.path.isfile(on_disk):
            raise ProvenanceError(
                "entry_missing",
                "das Restore-Werkzeug fehlt im Klon des Buendels",
                detail=f"{rel} bei {commit[:12]}")

    # -- 6: stammt es aus GENAU diesem Buendel? ----------------------------
    # Der Vergleich gegen das Blob im Klon ist der Riegel gegen einen Rueckfall
    # auf Code des laufenden Rechners: wer hier eine Datei ausserhalb des Klons
    # unterschiebt, faellt auf.
    entry_disk = os.path.join(clone, ENTRY_PATH)
    entry_sha = sha256_file(entry_disk)
    blob = subprocess.run(
        [_GB.resolve(), "-C", clone, "cat-file", "blob", f"{commit}:{ENTRY_PATH}"],
        capture_output=True, check=False, timeout=120.0)
    if blob.returncode != 0:
        raise ProvenanceError("entry_missing",
                              "das Werkzeug ist im Commit nicht auffindbar",
                              detail=f"{ENTRY_PATH} bei {commit[:12]}")
    if hashlib.sha256(blob.stdout).hexdigest() != entry_sha:
        raise ProvenanceError(
            "entry_foreign",
            "das geprüfte Werkzeug stammt nicht aus dem Buendel",
            detail=ENTRY_PATH)

    ref_name = next((name for target, name in refs if target == commit), "")
    proof = Proof(producer_commit=commit, producer_branch=branch,
                  producer_dirty=dirty, producer_root=root,
                  core_bundle_commit=commit, core_bundle_ref=ref_name,
                  bundle_sha256=sha256_file(bundle_path),
                  bundle_refs=len(refs), entry_sha256=entry_sha,
                  checked_paths=tuple(REQUIRED_PATHS))
    log.info("offsite.provenance_proven", commit=commit[:12],
             ref=ref_name or "(nur ueber Commit-ID)", refs=len(refs))
    return proof


def prove_staging_set(staging_set: str,
                      scratch_root: str | None = None) -> Proof:
    """Der Beweis fuer einen fertigen Sicherungssatz, wie der Job ihn braucht."""
    commit, _branch, _dirty = producer_revision()
    return prove_bundle(os.path.join(staging_set, BUNDLE_RELPATH),
                        expected_commit=commit, scratch_root=scratch_root)
