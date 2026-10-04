"""Checkpoint Publisher — die einzige Naht vom Klon in den kanonischen Speicher.

Der Builder bekommt **kein** Schreibrecht auf `/Users/solvio/solvio-core` und
keines auf dessen Refs. Er sieht den Pfad nicht einmal: seine Kindumgebung
traegt sechs Namen aus der Allowlist des Starters. Was hier passiert, tut
ausschliesslich Core-Code, und es tut genau eine Sache.

**Warum es diese Naht ueberhaupt gibt (DEBT-0179).** Der Arbeitsbereich des
Autopiloten ist ein isolierter Klon; das Offsite-Herkunftstor (§8.1) verlangt
den ausfuehrenden Commit im Buendel aus `inventory.CORE_REPO`. In einem Klon
ist er das nie — drei Offsite-Suiten konnten dort strukturell nicht gruen
werden. Statt das Tor aufzuweichen oder `CORE_REPO` umzubiegen, wird der
Commit **erreichbar gemacht**: ein unveraenderlicher Ref unter
`refs/autopilot/`, den `git bundle --all` mitnimmt.

**Die Mechanik, gemessen am 2026-09-01:**

    git -C <kanonisch> fetch --no-tags --no-write-fetch-head <klon> <commit>
    git -C <kanonisch> update-ref refs/autopilot/<m>/<c> <commit> ""

Der erste Schritt uebertraegt Objekte und erzeugt **null Refs**. Der zweite
legt genau einen an — und das leere Alt-Argument macht die Unveraenderlichkeit
zu einer **git-Sperre**, nicht zu einem Lesen-dann-Schreiben mit Zeitfenster.
Ein zweiter Versuch endet mit `cannot lock ref: reference already exists`.

**Die Autoritaetsgrenze ist der Refname.** Er wird nie entgegengenommen,
sondern aus zwei validierten Bestandteilen konstruiert. Ein Bestandteil mit
`/`, `..` oder `refs` existiert nicht — er faellt am Muster. Zusaetzlich prueft
eine Sperrliste den FERTIGEN Namen. Zwei Riegel, weil der eine ein Muster und
der andere eine Liste ist und beide anders versagen.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio import git_binary as _GB

log = get_logger("autopilot")

#: Der EINZIGE Namensraum, in den veroeffentlicht werden darf.
NAMESPACE = "refs/autopilot/"

#: Bestandteile eines Refnamens. Kein Schraegstrich, kein Punkt, kein Doppel-
#: punkt — damit ist `refs/heads/main` als Bestandteil unkonstruierbar.
_PART = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

#: Die Sperrliste gegen den FERTIGEN Namen. Verteidigung in der Tiefe: auch
#: wenn jemand das Muster spaeter lockert, fallen diese Praefixe.
FORBIDDEN_PREFIXES = ("refs/heads/", "refs/tags/", "refs/remotes/",
                      "refs/notes/", "refs/stash")
FORBIDDEN_EXACT = ("HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD")

#: Was der Publisher im kanonischen Repo ausfuehren darf. Geschlossen.
#: `push`, `checkout`, `switch`, `reset`, `tag`, `branch`, `merge` und `gc`
#: stehen NICHT darin und sind damit strukturell unmoeglich, nicht bloss
#: unerwuenscht.
ALLOWED_SUBCOMMANDS = frozenset({
    "fetch", "update-ref", "rev-parse", "for-each-ref", "status", "cat-file",
})

#: Argumente, die nie vorkommen duerfen — egal in welchem Unterbefehl.
FORBIDDEN_ARGS = ("--force", "-f", "--mirror", "--prune", "--delete", "-d")

FETCH_TIMEOUT = 600.0


class PublishRefused(RuntimeError):
    """Eine Publikation, die nicht stattfindet. Mit Grund."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Publication:
    """Was veroeffentlicht wurde — und der Beweis, dass sonst nichts geschah."""

    milestone_id: str
    checkpoint_id: str
    commit: str
    canonical_ref: str
    published_at: float
    untouched: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"milestone_id": self.milestone_id,
                "checkpoint_id": self.checkpoint_id,
                "commit": self.commit,
                "canonical_ref": self.canonical_ref,
                "published_at": self.published_at,
                "untouched": dict(self.untouched)}


# -- Der Refname: konstruiert, nie entgegengenommen ---------------------------
def build_ref(milestone_id: str, checkpoint_id: str) -> str:
    """Baut den Refnamen aus zwei validierten Bestandteilen.

    Es gibt bewusst **keine** Funktion, die einen fertigen Refnamen annimmt.
    Wer einen wollte, muesste diese hier umgehen — und das faellt in einer
    Durchsicht auf, waehrend ein durchgereichter Parameter nicht auffaellt.
    """
    for name, teil in (("milestone_id", milestone_id),
                       ("checkpoint_id", checkpoint_id)):
        if not isinstance(teil, str) or not _PART.match(teil):
            raise PublishRefused("bad_ref_component", f"{name}={teil!r}"[:80])
    ref = f"{NAMESPACE}{milestone_id}/{checkpoint_id}"
    assert_safe_ref(ref)
    return ref


def assert_safe_ref(ref: str) -> None:
    """Der zweite Riegel, gegen den fertigen Namen.

    **Die Reihenfolge ist der Unterschied zwischen einem Riegel und einer
    Verzierung.** Stuende die Namensraum-Pruefung zuerst, faenge sie jeden
    verbotenen Namen ab — und die Sperrliste waere unerreichbarer Code, der wie
    Sicherheit aussieht. Eine Mutation hat genau das gezeigt: sie zu entfernen
    aenderte nichts, weil niemand je an ihr vorbeikam.

    Also zuerst die Sperrliste, dann der Namensraum. Jetzt hat jeder Riegel
    einen Fall, den nur er faengt, und beide sind einzeln stellbar.
    """
    if ref in FORBIDDEN_EXACT:
        raise PublishRefused("forbidden_ref", ref[:80])
    for praefix in FORBIDDEN_PREFIXES:
        if ref.startswith(praefix):
            raise PublishRefused("forbidden_ref_prefix", ref[:80])
    if not ref.startswith(NAMESPACE):
        raise PublishRefused("ref_outside_namespace", ref[:80])
    if ".." in ref or ref.endswith("/") or "//" in ref:
        raise PublishRefused("malformed_ref", ref[:80])
    if len(ref.split("/")) != 4:
        raise PublishRefused("malformed_ref", ref[:80])


def new_checkpoint_id(*, now: float = 0.0) -> str:
    """Core-erzeugt. Ein Modell schlaegt keine Checkpoint-Kennung vor."""
    import uuid
    stempel = time.strftime("%Y%m%dt%H%M%S",
                            time.gmtime(now or time.time()))
    return f"cp-{stempel}-{uuid.uuid4().hex[:6]}"


# -- git, eng gefuehrt --------------------------------------------------------
def _git(repo: str, *args: str, timeout: float = 120.0,
         check: bool = True) -> subprocess.CompletedProcess:
    """git im kanonischen Repo — nur aus der Erlaubnisliste.

    Die Pruefung steht HIER und nicht bei den Aufrufern: eine Grenze, die
    jeder Aufrufer selbst einhalten muss, ist keine.
    """
    if not args or args[0] not in ALLOWED_SUBCOMMANDS:
        raise PublishRefused("forbidden_subcommand",
                             (args[0] if args else "")[:40])
    verboten = sorted(set(args) & set(FORBIDDEN_ARGS))
    if verboten:
        raise PublishRefused("forbidden_argument", ", ".join(verboten))
    for arg in args:
        if arg.startswith("+") and ":" in arg:
            raise PublishRefused("forced_refspec", arg[:60])
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    proc = subprocess.run([_GB.resolve(), "-C", repo, *args], env=env, timeout=timeout,
                          capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise PublishRefused("git_failed",
                             f"{args[0]}: {proc.stderr.strip()[:200]}")
    return proc


def snapshot(repo: str) -> dict[str, Any]:
    """Der Zustand, gegen den nachher geprueft wird. Aus git, nicht aus Hoffnung."""
    refs = {}
    for zeile in _git(repo, "for-each-ref",
                      "--format=%(refname) %(objectname)").stdout.splitlines():
        teile = zeile.split()
        if len(teile) == 2:
            refs[teile[0]] = teile[1]
    return {
        "head": _git(repo, "rev-parse", "HEAD").stdout.strip(),
        "head_ref": _git(repo, "rev-parse", "--abbrev-ref",
                         "HEAD").stdout.strip(),
        "refs": refs,
        "status": _git(repo, "status", "--porcelain").stdout,
    }


def _diff_snapshots(vorher: dict, nachher: dict, *, erlaubt: str) -> dict:
    """Was sich veraendert hat — genau ein neuer Ref darf es sein."""
    neu = {k: v for k, v in nachher["refs"].items() if k not in vorher["refs"]}
    fort = {k: v for k, v in vorher["refs"].items() if k not in nachher["refs"]}
    bewegt = {k: (vorher["refs"][k], v) for k, v in nachher["refs"].items()
              if k in vorher["refs"] and vorher["refs"][k] != v}
    return {
        "head_unchanged": vorher["head"] == nachher["head"],
        "head_ref_unchanged": vorher["head_ref"] == nachher["head_ref"],
        "worktree_unchanged": vorher["status"] == nachher["status"],
        "new_refs": sorted(neu),
        "removed_refs": sorted(fort),
        "moved_refs": sorted(bewegt),
        "only_expected_ref": sorted(neu) == [erlaubt] and not fort and not bewegt,
    }


class CheckpointPublisher:
    """Veroeffentlicht EINEN geprueften Commit unter EINEM konstruierten Ref."""

    def __init__(self, canonical_repo: str = "", *, scanner=None) -> None:
        from solvio.storage import inventory
        self.canonical = os.path.abspath(os.path.expanduser(
            canonical_repo or inventory.CORE_REPO))
        self.scanner = scanner

    # -- Vorbedingungen ------------------------------------------------------
    def _assert_clone(self, clone: str, commit: str) -> None:
        if not os.path.isdir(os.path.join(clone, ".git")):
            raise PublishRefused("clone_not_a_repository", clone)
        if os.path.realpath(clone) == os.path.realpath(self.canonical):
            # Ein „Klon", der der kanonische Baum IST, waere kein Klon.
            raise PublishRefused("clone_is_canonical", clone)
        vorhanden = subprocess.run(
            [_GB.resolve(), "-C", clone, "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True, check=False)
        if vorhanden.returncode != 0:
            raise PublishRefused("commit_absent_in_clone", commit[:12])
        schmutzig = subprocess.run([_GB.resolve(), "-C", clone, "status", "--porcelain"],
                                   capture_output=True, text=True, check=False)
        if schmutzig.stdout.strip():
            raise PublishRefused(
                "clone_not_normalised",
                f"{len(schmutzig.stdout.splitlines())} offene Aenderung(en)")
        kopf = subprocess.run([_GB.resolve(), "-C", clone, "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False)
        if kopf.stdout.strip() != commit:
            # Der veroeffentlichte Commit MUSS der sein, den das Gate prueft.
            raise PublishRefused("commit_is_not_head",
                                 f"{commit[:12]} != {kopf.stdout.strip()[:12]}")

    def _assert_clean(self, clone: str, commit: str) -> None:
        """Der Kredential-Scan. Rot heisst: es entsteht KEIN Ref."""
        pruefer = self.scanner or _default_scanner
        befunde = list(pruefer(clone, commit) or [])
        if befunde:
            raise PublishRefused("credential_in_checkpoint",
                                 ", ".join(befunde[:10]))

    # -- Der Vorgang ---------------------------------------------------------
    def publish(self, *, clone: str, commit: str, milestone_id: str,
                checkpoint_id: str = "", contract_hash: str = "",
                expected_contract_hash: str = "",
                now: float = 0.0) -> Publication:
        """Einen Checkpoint erreichbar machen. Alles andere bleibt, wie es war."""
        if expected_contract_hash and contract_hash != expected_contract_hash:
            raise PublishRefused("contract_drift",
                                 f"{contract_hash[:20]} != "
                                 f"{expected_contract_hash[:20]}")
        kennung = checkpoint_id or new_checkpoint_id(now=now)
        ref = build_ref(milestone_id, kennung)

        self._assert_clone(clone, commit)
        self._assert_clean(clone, commit)

        vorher = snapshot(self.canonical)
        if ref in vorher["refs"]:
            raise PublishRefused("checkpoint_ref_exists", ref)

        # 1) Objekte holen — ohne einen einzigen Ref zu schreiben.
        _git(self.canonical, "fetch", "--no-tags", "--no-write-fetch-head",
             clone, commit, timeout=FETCH_TIMEOUT)
        # 2) Genau einen Ref anlegen. Das leere Alt-Argument ist die Sperre:
        #    existiert er schon, scheitert git — ohne Zeitfenster.
        _git(self.canonical, "update-ref", ref, commit, "")

        nachher = snapshot(self.canonical)
        unberuehrt = _diff_snapshots(vorher, nachher, erlaubt=ref)
        if not unberuehrt["only_expected_ref"]:
            raise PublishRefused("unexpected_ref_change", str(unberuehrt)[:200])
        if not (unberuehrt["head_unchanged"] and unberuehrt["worktree_unchanged"]
                and unberuehrt["head_ref_unchanged"]):
            raise PublishRefused("canonical_tree_changed", str(unberuehrt)[:200])
        if nachher["refs"].get(ref) != commit:
            raise PublishRefused("ref_points_elsewhere",
                                 str(nachher["refs"].get(ref))[:40])

        moment = now or time.time()
        log.info("autopilot.checkpoint_published", milestone=milestone_id,
                 checkpoint=kennung, commit=commit[:12], ref=ref)
        return Publication(milestone_id=milestone_id, checkpoint_id=kennung,
                           commit=commit, canonical_ref=ref,
                           published_at=moment, untouched=unberuehrt)

    # -- Auskunft ------------------------------------------------------------
    def reachable(self, commit: str) -> bool:
        """Ist der Commit im kanonischen Repo erreichbar? Genau die Frage, die
        das Offsite-Herkunftstor spaeter stellt."""
        proc = _git(self.canonical, "cat-file", "-e", f"{commit}^{{commit}}",
                    check=False)
        return proc.returncode == 0

    def published(self, milestone_id: str) -> list[tuple[str, str]]:
        proc = _git(self.canonical, "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    f"{NAMESPACE}{milestone_id}/")
        out = []
        for zeile in proc.stdout.splitlines():
            teile = zeile.split()
            if len(teile) == 2:
                out.append((teile[0], teile[1]))
        return out


def _default_scanner(clone: str, commit: str) -> list[str]:
    """Derselbe Scan wie beim Checkpoint — Name UND Inhalt der Aenderungen.

    Der Bereich wird an `_scan_staged` durchgereicht, damit dort dieselbe
    Unterscheidung gilt wie beim Checkpoint: eine bekannte Datei wird an dem
    gemessen, was HINZUKAM, eine neue an ihrem ganzen Inhalt (DEBT-0189).

    Ein Wurzel-Commit hat keinen Vorgaenger. Frueher stand dann der Commit
    allein im Bereich — und `git diff <commit>` vergleicht den Arbeitsbaum
    gegen ihn, was in einem sauberen Klon leer ist. Der Scan lief also durch,
    ohne etwas gesehen zu haben. Gegen den leeren Baum ist alles neu.
    """
    from solvio.autopilot.builders import EMPTY_TREE, _scan_staged
    eltern = subprocess.run(
        [_GB.resolve(), "-C", clone, "rev-parse", f"{commit}^"],
        capture_output=True, text=True, check=False).stdout.strip()
    bereich = f"{eltern or EMPTY_TREE}..{commit}"
    proc = subprocess.run(
        [_GB.resolve(), "-C", clone, "diff", "--no-renames", "--name-only",
         "--diff-filter=AM", bereich],
        capture_output=True, text=True, check=False)
    pfade = [z.strip() for z in proc.stdout.splitlines() if z.strip()]
    return list(_scan_staged(clone, pfade, bereich=bereich))
