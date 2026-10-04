"""Arbeitsbereiche: ein Klon je Lauf — und der Produktivbaum bleibt unberuehrt.

Die Zusicherungen hier sind alle strukturell, keine davon ist eine Absicht:

* der Klon traegt keine **Alternates** und keine **geteilten Inodes** — der eine
  waere ein Blick in das Produktiv-`.git`, der andere ein Schreiber, der es in
  place beschaedigt;
* `.env` ist unversioniert und deshalb **strukturell abwesend**, nicht gefiltert;
* die Repo-Allowlist ist realpath-geprueft — `..` und Symlink-Flucht fallen
  durch (uebernommen aus dem stillgelegten `test_codex_security.py`, dessen
  uebertragbare Zusicherungen hier weiterleben);
* das Zielrepo ist vor und nach der Ernte **byte-identisch**, inklusive
  Ref-Liste: ein Fetch in dessen Refs wuerde bei jedem Lauf den Ref-Digest der
  Sicherung kippen;
* ein nicht-sauberer Bereich wird **gemeldet**, nicht still entsorgt.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from functools import wraps
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-ws-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import store as S  # noqa: E402
from solvio.agent_runtime import workspace as W  # noqa: E402


def _git(*args, cwd):
    env = {"PATH": "/usr/bin:/bin", "HOME": cwd, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["/usr/bin/git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def _source_repo(with_env: bool = True) -> str:
    """Ein Quellrepo, das aussieht wie das echte: versionierter Code plus eine
    unversionierte `.env` daneben."""
    folder = tempfile.mkdtemp(prefix="solvio-src-")
    _git("init", "-q", "-b", "main", cwd=folder)
    with open(os.path.join(folder, "code.py"), "w", encoding="utf-8") as handle:
        handle.write("print('hallo')\n")
    with open(os.path.join(folder, ".gitignore"), "w", encoding="utf-8") as handle:
        handle.write(".env\n")
    _git("add", "-A", cwd=folder)
    _git("commit", "-q", "-m", "start", cwd=folder)
    if with_env:
        # Unversioniert und absichtlich: genau darum geht es.
        with open(os.path.join(folder, ".env"), "w", encoding="utf-8") as handle:
            handle.write("OPENAI_API_KEY=sk-CANARY0000000000000000000\n")
    return os.path.realpath(folder)


def _manager(source: str) -> W.WorkspaceManager:
    return W.WorkspaceManager(allowed=(source,))


def _fingerprint(repo: str) -> tuple[str, str]:
    """Was sich am Zielrepo nicht aendern darf: Arbeitsbaumstatus und Ref-Liste."""
    status = _git("status", "--porcelain", cwd=repo).stdout
    refs = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo).stdout
    return status, refs


# =====================================================================
# Der Klon
# =====================================================================

def t_the_clone_carries_no_alternates_and_no_shared_inodes():
    """Ein Alternates-Klon zeigt in das Produktiv-`.git` hinein; ein
    Hardlink-Klon teilt Inodes, und ein Schreiber beschaedigt das Original in
    place. Beides ist hier strukturell ausgeschlossen."""
    source = _source_repo()
    workspace = _manager(source).clone("ar-clone01", source)
    alternates = os.path.join(workspace.path, ".git", "objects", "info", "alternates")
    require(not os.path.exists(alternates), "der Klon traegt Alternates")

    objects = os.path.join(workspace.path, ".git", "objects")
    shared = []
    for base, _dirs, files in os.walk(objects):
        for name in files:
            path = os.path.join(base, name)
            if os.stat(path).st_nlink > 1:
                shared.append(path)
    require_equal(shared, [], "der Klon teilt Inodes mit dem Quellrepo")


def t_an_unversioned_env_is_structurally_absent_from_the_clone():
    """Nicht gefiltert — abwesend. Ein Klon enthaelt nur Versioniertes."""
    source = _source_repo(with_env=True)
    require(os.path.exists(os.path.join(source, ".env")), "das Quellrepo hat keine .env")
    workspace = _manager(source).clone("ar-clone02", source)
    require(not os.path.exists(os.path.join(workspace.path, ".env")),
            "die .env ist im Arbeitsbereich gelandet")


def t_the_clone_keeps_no_remote_back_to_production():
    """Ein `git push origin` von innen waere eine Produktivmutation."""
    source = _source_repo()
    workspace = _manager(source).clone("ar-clone03", source)
    remotes = _git("remote", cwd=workspace.path).stdout.strip()
    require_equal(remotes, "", f"der Klon behielt ein Fernziel: {remotes}")


def t_the_clone_starts_on_its_own_agent_branch():
    source = _source_repo()
    workspace = _manager(source).clone("ar-clone04", source, slug="reparatur")
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace.path).stdout.strip()
    require_equal(current, workspace.branch, "der Klon steht nicht auf dem Agentenzweig")
    require(current.startswith("agent/"), f"kein Agentenzweig: {current}")


# =====================================================================
# N3 — Wiederaufnahme prueft den gebundenen Klon und jede Core-Ernte erneut
# =====================================================================

def _restore(manager, workspace, **changed):
    return manager.restore(**{**asdict(workspace), "requested_repo": workspace.repo,
                              **changed})


def _isolated_workspace_state(case):
    """Hostile, deliberately dirty clones must not pollute the legacy sweeper test."""
    @wraps(case)
    def run():
        with tempfile.TemporaryDirectory(prefix="solvio-restore-state-") as folder:
            with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder,
                    "SOLVIO_AGENT_RUNS_DB": os.path.join(folder, "runs.sqlite3")}):
                return case()
    return run


@_isolated_workspace_state
def t_restore_preserves_the_original_base_and_harvests_existing_work():
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-restore-valid", source)
    inode = os.stat(workspace.path).st_ino
    # Das Zielrepo darf inzwischen weitergelaufen sein. Sein heutiges HEAD
    # ist keine neue Base fuer diesen bereits gebundenen Arbeitsbereich.
    Path(source, "spaeter.py").write_text("print('anderer Auftrag')\n")
    require_equal(_git("add", "-A", cwd=source).returncode, 0)
    require_equal(_git("commit", "-q", "-m", "spaeter", cwd=source).returncode, 0)
    require(_git("rev-parse", "HEAD", cwd=source).stdout.strip() != workspace.base)
    source_before = _fingerprint(source)
    Path(workspace.path, "wiederaufgenommen.py").write_text("print('belegtes Ergebnis')\n")
    restored = _restore(_manager(source), workspace)
    require_equal(asdict(restored), asdict(workspace))
    require_equal(os.stat(restored.path).st_ino, inode, "Restore hat einen zweiten Klon angelegt")
    ref = manager.harvest(restored)
    result = _git("show", ref + ":wiederaufgenommen.py", cwd=W.harvest_path())
    require_equal(result.returncode, 0)
    require_equal(result.stdout, "print('belegtes Ergebnis')\n")
    require_equal(_fingerprint(source), source_before)


@_isolated_workspace_state
def t_restore_refuses_changed_branch_and_missing_or_unrelated_base():
    for corruption in ("branch", "missing_base", "unrelated_base"):
        source = _source_repo()
        manager = _manager(source)
        workspace = manager.clone("ar-restore-" + corruption, source)
        changed = {}
        if corruption == "branch":
            result = _git("checkout", "-q", "-b", "agent/other-" + workspace.run_id,
                          cwd=workspace.path)
            require_equal(result.returncode, 0)
            expected = "workspace_branch_changed"
        elif corruption == "missing_base":
            changed["base"] = ""
            expected = "workspace_binding_invalid"
        else:
            tree = _git("rev-parse", "HEAD^{tree}", cwd=workspace.path).stdout.strip()
            # Existiert als Commit im selben Objektbestand, ist aber KEIN
            # Vorfahr des Branches. Existenz allein waere keine Herkunft.
            result = _git("commit-tree", tree, "-m", "unrelated root", cwd=workspace.path)
            require_equal(result.returncode, 0)
            changed["base"] = result.stdout.strip()
            expected = "workspace_base_changed"
        error = require_raises(W.WorkspaceError, _restore, manager, workspace, **changed)
        require_equal(error.reason, expected, corruption)
        require(not any(workspace.run_id in ref for ref in manager.harvest_refs()))


@_isolated_workspace_state
def t_restore_refuses_symlink_workspaces_and_external_git_indirection():
    for corruption in ("workspace_symlink", "git_symlink", "git_file"):
        source = _source_repo()
        manager = _manager(source)
        workspace = manager.clone("ar-restore-" + corruption, source)
        original = _fingerprint(source)
        if corruption == "workspace_symlink":
            moved = workspace.path + "-moved"
            os.rename(workspace.path, moved)
            os.symlink(moved, workspace.path)
            expected = "workspace_path_changed"
        else:
            gitdir = os.path.join(workspace.path, ".git")
            os.rename(gitdir, gitdir + "-original")
            if corruption == "git_symlink":
                os.symlink(os.path.join(source, ".git"), gitdir)
            else:
                Path(gitdir).write_text("gitdir: " + os.path.join(source, ".git") + "\n")
            expected = "workspace_git_not_local"
        error = require_raises(W.WorkspaceError, _restore, manager, workspace)
        require_equal(error.reason, expected, corruption)
        require_equal(_fingerprint(source), original)


@_isolated_workspace_state
def t_core_worktree_configuration_cannot_redirect_restore_or_harvest():
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-restore-worktree-config", source)
    with tempfile.TemporaryDirectory(prefix="solvio-wrong-worktree-") as outside:
        Path(outside, "fremd.txt").write_text("kein Arbeitsmaterial dieses Auftrags\n")
        config = _git("config", "--local", "core.worktree", outside, cwd=workspace.path)
        require_equal(config.returncode, 0)
        # Belegt den Angriff am echten Git: gemeinsame .git-Daten, Branch
        # und Base bleiben gleich; der Arbeitsbaum zeigt trotzdem nach draussen.
        require_equal(_git("rev-parse", "--show-toplevel", cwd=workspace.path).stdout.strip(),
                      os.path.realpath(outside))
        require_equal(_git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip(), workspace.base)
        for action in (lambda: _restore(manager, workspace), lambda: manager.harvest(workspace)):
            error = require_raises(W.WorkspaceError, action)
            require_equal(error.reason, "workspace_git_config_untrusted")
        require_equal(Path(outside, "fremd.txt").read_text(), "kein Arbeitsmaterial dieses Auftrags\n")
        require(not any(workspace.run_id in ref for ref in manager.harvest_refs()))


@_isolated_workspace_state
def t_local_filters_includes_signing_and_hooks_are_refused_before_core_git_effects():
    for feature in ("clean", "process", "include", "signing", "hooks"):
        source = _source_repo()
        manager = _manager(source)
        workspace = manager.clone("ar-restore-config-" + feature, source)
        with tempfile.TemporaryDirectory(prefix="solvio-git-helper-") as outside:
            marker = Path(outside, "executed.txt")
            helper = Path(outside, "helper")
            # A real executable, deliberately outside the permitted workspace.
            # It exits immediately even for the filter-process protocol, so an
            # accidentally started helper can never hang this probe.
            helper.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                              f"Path({str(marker)!r}).write_text('executed')\nraise SystemExit(1)\n")
            helper.chmod(0o700)
            Path(workspace.path, "neu.txt").write_text("zu pruefende Aenderung\n")
            tracked = Path(workspace.path, "code.py")
            tracked.write_text("print('bello')\n")  # same size as the tracked original
            changed_stat = tracked.stat()
            os.utime(tracked, (changed_stat.st_atime, changed_stat.st_mtime + 5))
            pairs = []
            if feature in {"clean", "process"}:
                Path(workspace.path, ".gitattributes").write_text(
                    "neu.txt filter=probe\ncode.py filter=probe\n")
                pairs = [("filter.probe." + feature, str(helper))]
            elif feature == "include":
                included = Path(outside, "included.gitconfig")
                included.write_text("[core]\n\tfsmonitor = " + str(helper) + "\n")
                pairs = [("include.path", str(included))]
            elif feature == "signing":
                pairs = [("commit.gpgsign", "true"), ("gpg.program", str(helper))]
            else:
                hooks = Path(outside, "hooks")
                hooks.mkdir()
                shutil.copy2(helper, hooks / "pre-commit")
                pairs = [("core.hooksPath", str(hooks))]
            for key, value in pairs:
                require_equal(_git("config", "--local", key, value, cwd=workspace.path).returncode, 0)
            for action in (lambda: _restore(manager, workspace), lambda: manager.harvest(workspace)):
                error = require_raises(W.WorkspaceError, action)
                require_equal(error.reason, "workspace_git_config_untrusted", feature)
                require(not marker.exists(), feature + ": Core executed a local Git helper")
            # A same-size tracked edit forces status to inspect content. Thus
            # cleanup is itself an execution boundary for clean/process filters,
            # even after restore/harvest have correctly refused this directory.
            require_equal(manager.is_clean(workspace.path), False)
            require_equal(manager.cleanup(workspace.run_id), "unclean")
            require(os.path.isdir(workspace.path), "refused Git state was silently removed")
            require(not marker.exists(), feature + ": Core cleanup executed a Git helper")
            require_equal(_git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip(), workspace.base)
            require(not any(workspace.run_id in ref for ref in manager.harvest_refs()))
            # Positive control of the attack fixture: ordinary unguarded Git
            # really invokes this helper. This is a separate, explicit local
            # probe AFTER the Core refused it; only the temporary marker changes.
            if feature in {"clean", "process"}:
                _git("add", "neu.txt", cwd=workspace.path)
            elif feature == "include":
                _git("status", "--porcelain", cwd=workspace.path)
            else:
                require_equal(_git("add", "neu.txt", cwd=workspace.path).returncode, 0)
                _git("commit", "-q", "-m", "unguarded control", cwd=workspace.path)
            require(marker.exists(), feature + ": the local helper probe was not armed")


# =====================================================================
# Die Allowlist
# =====================================================================

def t_the_repository_allowlist_is_realpath_checked():
    """`..` und Symlink-Flucht fallen durch — uebernommen aus der Suite des
    stillgelegten Codex-Agenten."""
    source = _source_repo()
    manager = _manager(source)
    require_equal(manager.resolve_repo(source), source, "das erlaubte Repo faellt durch")

    outside = tempfile.mkdtemp(prefix="solvio-outside-")
    require_raises(W.WorkspaceError, manager.resolve_repo, outside,
                   message="ein fremdes Verzeichnis wurde erlaubt")
    require_raises(W.WorkspaceError, manager.resolve_repo,
                   os.path.join(source, "..", os.path.basename(outside)),
                   message="ein ..-Pfad entkam der Allowlist")

    link = os.path.join(tempfile.mkdtemp(prefix="solvio-link-"), "zeiger")
    os.symlink(outside, link)
    require_raises(W.WorkspaceError, manager.resolve_repo, link,
                   message="ein Symlink entkam der Allowlist")


def t_a_symlink_that_points_INTO_the_allowlist_is_accepted_by_its_real_path():
    """Die Gegenprobe: `realpath` soll aufloesen, nicht bloss ablehnen."""
    source = _source_repo()
    link = os.path.join(tempfile.mkdtemp(prefix="solvio-link2-"), "zeiger")
    os.symlink(source, link)
    require_equal(_manager(source).resolve_repo(link), source,
                  "ein Symlink auf das erlaubte Repo wurde abgelehnt")


# =====================================================================
# Die Ernte
# =====================================================================

def t_the_target_repository_is_byte_identical_before_and_after_a_harvest():
    """Ein Fetch in die Refs des Zielrepos wuerde bei jedem Lauf den Ref-Digest
    der Sicherung kippen — und `refs/agents/*` liefen dort unbegrenzt auf."""
    source = _source_repo()
    manager = _manager(source)
    before = _fingerprint(source)

    workspace = manager.clone("ar-harv01", source)
    with open(os.path.join(workspace.path, "neu.py"), "w", encoding="utf-8") as handle:
        handle.write("print('arbeit')\n")
    _git("add", "-A", cwd=workspace.path)
    _git("commit", "-q", "-m", "arbeit", cwd=workspace.path)

    ref = manager.harvest(workspace)
    require(ref.startswith("refs/agents/"), f"falscher Ref: {ref}")

    after = _fingerprint(source)
    require_equal(after, before, "das Zielrepo hat sich veraendert")
    require(not any("agents" in line for line in after[1].splitlines()),
            "im Zielrepo stehen Agenten-Refs")


def t_the_harvest_lands_in_the_cores_own_bare_repository():
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-harv02", source)
    with open(os.path.join(workspace.path, "neu.py"), "w", encoding="utf-8") as handle:
        handle.write("print('arbeit')\n")
    _git("add", "-A", cwd=workspace.path)
    _git("commit", "-q", "-m", "arbeit", cwd=workspace.path)
    manager.harvest(workspace)

    refs = manager.harvest_refs()
    require(any("ar-harv02" in ref for ref in refs), f"der Ref fehlt: {refs}")
    require(W.harvest_path().endswith("agent_harvest.git"), "falsches Ernte-Repo")
    require(os.path.isdir(os.path.join(W.harvest_path(), "objects")),
            "das Ernte-Repo ist kein bare-Repo")


def t_work_the_builder_left_uncommitted_still_becomes_the_result():
    """Der unehrlichste Fehler dieses Milestones, als Test.

    Der Builder legte die Datei an und committete sie nicht. Die Ernte nahm den
    Zweig, wie er war — also den Klonpunkt —, meldete Erfolg, und SOLVIO sagte
    dem Nutzer „das Ergebnis liegt bereit". Es lag nichts bereit.

    Die Ernte macht das Festhalten jetzt selbst, statt es einem fremden Werkzeug
    zu ueberlassen.
    """
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-uncommitted", source)
    with open(os.path.join(workspace.path, "Notiz.md"), "w", encoding="utf-8") as handle:
        handle.write("Testnotiz aus dem Agent-Runtime\n")
    require(not manager.is_clean(workspace.path), "der Aufbau stimmt nicht")

    ref = manager.harvest(workspace)
    geerntet = _git("rev-parse", ref, cwd=W.harvest_path()).stdout.strip()
    require(geerntet != workspace.base,
            "die Ernte zeigt auf den Klonpunkt — die Arbeit fehlt")
    inhalt = _git("show", f"{ref}:Notiz.md", cwd=W.harvest_path()).stdout
    require_equal(inhalt, "Testnotiz aus dem Agent-Runtime\n",
                  "die Datei kam nicht mit oder kam veraendert an")


def t_a_branch_that_never_moved_is_refused_instead_of_harvested():
    """Die zweite, unabhaengige Sicherung.

    Selbst wenn nichts festgehalten wurde: eine Ernte, die auf den Klonpunkt
    zeigt, ist keine Ernte. Sie sah live wie ein Erfolg aus.
    """
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-untouched", source)
    require(manager.is_clean(workspace.path), "der Aufbau stimmt nicht")

    error = require_raises(W.NothingToHarvest, manager.harvest, workspace,
                           message="ein unveraenderter Zweig wurde geerntet")
    require_equal(error.reason, "no_change", "falscher Grund")
    require(not any("ar-untouched" in r for r in manager.harvest_refs()),
            "eine verweigerte Ernte hat doch eine Ref geschrieben")


def t_uncommitted_credentials_are_scanned_before_they_are_harvested():
    """Das Festhalten darf die Kredentialgrenze nicht unterlaufen.

    Der Commit passiert VOR dem Scan — sonst pruefte der Scan an genau der
    Arbeit vorbei, die er pruefen soll. Diese Reihenfolge ist die ganze Zusage.
    """
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-poison-uncommitted", source)
    # Unversioniert hingelegt, wie es der Builder auch getan haette.
    with open(os.path.join(workspace.path, "mitgebracht.txt"), "w",
              encoding="utf-8") as handle:
        handle.write('{"OPENAI_API_KEY": "sk-CANARY0000000000000000000"}\n')

    require_raises(W.HarvestRefused, manager.harvest, workspace,
                   message="unversionierte Anmeldedaten kamen durch")
    require(not any("ar-poison-uncommitted" in r for r in manager.harvest_refs()),
            "die verweigerte Ernte hat doch eine Ref geschrieben")


def t_harvest_refs_follow_the_retention():
    source = _source_repo()
    manager = _manager(source)
    for index in (1, 2):
        workspace = manager.clone(f"ar-keep0{index}", source)
        with open(os.path.join(workspace.path, "n.py"), "w", encoding="utf-8") as handle:
            handle.write(f"# {index}\n")
        _git("add", "-A", cwd=workspace.path)
        _git("commit", "-q", "-m", "arbeit", cwd=workspace.path)
        manager.harvest(workspace)

    removed = manager.prune_refs(keep={"ar-keep01"})
    require(any("ar-keep02" in ref for ref in removed), f"nichts gepruent: {removed}")
    remaining = manager.harvest_refs()
    require(any("ar-keep01" in ref for ref in remaining), "der behaltene Ref ist fort")
    require(not any("ar-keep02" in ref for ref in remaining), "der gepruente Ref lebt")


def t_the_scan_looks_at_what_the_agent_changed_not_at_the_whole_tree():
    """Live gefunden, und der Fehler war teuer.

    Ein Scan ueber den GESAMTEN Baum fand im echten Repository 139 Dateien mit
    „Kredentialgestalt" — `.env.example`, das Schuldenregister, die ADRs, die
    Module dieser Laufzeit. Alles Text, der ueber Schluessel SCHREIBT. Die Ernte
    haette damit immer verweigert, und eine Pruefung, die immer verweigert,
    schuetzt nichts; sie wird abgeschaltet.

    Die Frage ist „hat der Agent Anmeldematerial hineingelegt", nicht „enthaelt
    dieses Repository irgendwo das Wort token".
    """
    source = _source_repo()
    manager = _manager(source)
    # Eine Datei, die im QUELLREPO ueber Anmeldung schreibt — wie jede Doku.
    with open(os.path.join(source, "HINWEISE.md"), "w", encoding="utf-8") as handle:
        handle.write("Das Feld heisst refresh_token und der Wert ist geheim.\n"
                     "Beispiel: eyJhbGciOiJIUzI1NiJ9.BEISPIELWERT00.SIGNATUR00\n")
    _git("add", "-A", cwd=source); _git("commit", "-q", "-m", "doku", cwd=source)

    workspace = manager.clone("ar-scan01", source)
    require(workspace.base, "der Klon merkt sich seinen Ausgangspunkt nicht")

    # Der Agent aendert etwas voellig Harmloses.
    with open(os.path.join(workspace.path, "neu.txt"), "w", encoding="utf-8") as handle:
        handle.write("harmlos\n")
    _git("add", "-A", cwd=workspace.path)
    _git("commit", "-q", "-m", "arbeit", cwd=workspace.path)

    befunde = manager.scan_branch(workspace.path, workspace.branch,
                                  base=workspace.base)
    require_equal(befunde, [],
                  f"vorbestehende Doku wurde dem Agenten angelastet: {befunde}")
    ref = manager.harvest(workspace)
    require(ref.endswith("ar-scan01"), "die saubere Arbeit wurde nicht geerntet")


def t_the_content_scan_uses_shapes_not_the_prose_predicate():
    """Das kontextuelle Praedikat des Hauses ist fuer AUSSAGEN gebaut — fuer
    Prosa, in der jemand „mein Passwort ist X" schreibt. Ein Quellbaum ist etwas
    anderes: dort steht `token` in jedem zweiten Kommentar."""
    from solvio.agent_runtime.specialists import redact_specialist_output

    code = ("def main():\n"
            "    # wir reden hier ueber tokens, secrets und credentials\n"
            "    password = get_from_vault()\n")
    require_equal(redact_specialist_output(code), code,
                  "harmloser Quellcode loest den Inhaltsscan aus")

    material = '{"tokens": {"refresh_token": "CANARY0refresh0value000000000000"}}'
    require(redact_specialist_output(material) != material,
            "echtes Anmeldematerial loest den Inhaltsscan NICHT aus")


def t_an_agent_that_adds_credential_material_is_still_refused():
    """Die Gegenprobe zur Entschaerfung: was der Agent HINZUFUEGT, wird gefangen —
    am Namen und am Inhalt."""
    source = _source_repo()
    manager = _manager(source)

    for name, body in (("auth.json", '{"tokens":{"refresh_token":"CANARY000000000000000000"}}'),
                       ("harmlos.txt", '{"tokens":{"refresh_token":"CANARY000000000000000000"}}')):
        workspace = manager.clone(f"ar-bad-{name.split('.')[0]}", source)
        with open(os.path.join(workspace.path, name), "w", encoding="utf-8") as handle:
            handle.write(body)
        _git("add", "-A", cwd=workspace.path)
        _git("commit", "-q", "-m", "gestohlen", cwd=workspace.path)
        befunde = manager.scan_branch(workspace.path, workspace.branch,
                                      base=workspace.base)
        require(befunde, f"{name} wurde nicht beanstandet")
        require_raises(W.HarvestRefused, manager.harvest, workspace,
                       message=f"{name} wurde geerntet")
        require(not any(workspace.run_id in r for r in manager.harvest_refs()),
                f"{name}: eine verweigerte Ernte hinterliess einen Ref")


# =====================================================================
# Aufraeumen
# =====================================================================

def t_an_unclean_workspace_is_reported_not_silently_removed():
    """„Da lagen noch Aenderungen" ist eine Auskunft, die ein Mensch haben will."""
    source = _source_repo()
    manager = _manager(source)
    workspace = manager.clone("ar-dirty01", source)
    with open(os.path.join(workspace.path, "unfertig.py"), "w", encoding="utf-8") as h:
        h.write("# haelfte\n")

    require_equal(manager.cleanup("ar-dirty01"), "unclean", "ein schmutziger Bereich flog")
    require(os.path.isdir(workspace.path), "der Bereich wurde doch entfernt")
    require_equal(manager.cleanup("ar-dirty01", force=True), "removed",
                  "auch mit force blieb er stehen")


def t_a_clean_workspace_is_removed():
    source = _source_repo()
    manager = _manager(source)
    manager.clone("ar-clean01", source)
    require_equal(manager.cleanup("ar-clean01"), "removed", "ein sauberer Bereich blieb")
    require(not os.path.isdir(W.workspace_root("ar-clean01")), "das Verzeichnis lebt")


def t_reconcile_removes_orphans_of_terminal_runs_and_reports_the_rest():
    source = _source_repo()
    manager = _manager(source)
    folder = tempfile.mkdtemp(prefix="solvio-ws-db-")
    ledger = S.AgentRunLedger(os.path.join(folder, "agent_runs.sqlite3"))
    task = ledger.create_task(objective="Ein Bauauftrag hier", scope=S.SCOPE_BUILD,
                              created_origin="local_owner", created_principal="o")
    run = ledger.create_run(task_id=task.task_id)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.SUCCEEDED)

    manager.clone(run.run_id, source)
    manager.clone("ar-unknown01", source)          # gehoert zu keinem Lauf
    stale = manager.reconcile(ledger)
    require(not os.path.isdir(W.workspace_root(run.run_id)),
            "der Bereich eines terminalen Laufs blieb")
    require(not os.path.isdir(W.workspace_root("ar-unknown01")),
            "ein herrenloser Bereich blieb")
    require_equal(stale, [], f"unerwartet gemeldet: {stale}")


def t_a_spoken_word_is_not_a_repository_path():
    """Live gefunden: der Auftrag „lege im Projekt eine Datei an" schrieb das
    blosse Wort `Projekt` als `target_repo` ins Buch. Daran zerbrach jeder
    Bauauftrag.

    Ein Wort ohne Schraegstrich ist keine Zielangabe. Es gilt als *nichts
    angegeben* — und dann greift der Standard, nicht ein geratener Ort.
    """
    source = _source_repo()
    manager = _manager(source)
    standard = manager.resolve_repo("")
    for wort in ("Projekt", "projekt", "solvio-core", "das Projekt", "  Projekt  "):
        require_equal(manager.resolve_repo(wort), standard,
                      f"{wort!r} gilt nicht als fehlende Angabe")
    require_equal(standard, source, "der Standard ist nicht die Allowlist-Wurzel")


def t_a_relative_path_is_refused_instead_of_guessed():
    """Der gefaehrlichere Teil desselben Fundes.

    `realpath` loest einen relativen Namen gegen das ARBEITSVERZEICHNIS des
    Dienstes auf — und das liegt im Produktivcheckout. Ein Wert wie `docs`
    haette damit IN die Allowlist gezeigt, und was geklont wird, haenge am cwd,
    der nirgends vereinbart ist. Ein relativer Pfad wird deshalb abgelehnt,
    nicht geraten.
    """
    manager = _manager(_source_repo())
    for relativ in ("../etc", "docs/architecture", "./x", "a/b"):
        error = require_raises(W.WorkspaceError, manager.resolve_repo, relativ,
                               message=f"{relativ!r} wurde aufgeloest statt abgelehnt")
        require_equal(error.reason, "repository_not_absolute",
                      f"{relativ!r} wurde mit falschem Grund abgelehnt")


def t_a_tilde_that_expanduser_does_not_resolve_is_still_relative():
    """Gefunden von einem adversarischen Test gegen die Regel darueber — und die
    erste Fassung dieser Regel fiel darauf herein.

    Sie fragte nach der ZEICHENFORM: „beginnt es mit `~`, dann ist es ein
    Home-Pfad." `~x/../docs` beginnt mit `~`, ist aber keiner: es existiert kein
    Benutzer `x`, `expanduser` laesst den Wert unveraendert stehen, und
    `realpath` loeste ihn dann doch gegen den cwd auf. Gemessen aus dem
    Produktiv-cwd ergab das ein echtes, klonbares Verzeichnis — darunter das
    `.git` des Produktivbaums.

    Entschieden wird deshalb am ERGEBNIS von `expanduser`, nicht am ersten
    Zeichen.
    """
    manager = _manager(_source_repo())
    for getarnt in ("~x/../docs", "~x/..", "~x/../.git", "~nichtvorhanden/../etc",
                    "~x/../../..", "~\t/../docs"):
        error = require_raises(W.WorkspaceError, manager.resolve_repo, getarnt,
                               message=f"{getarnt!r} wurde relativ aufgeloest")
        require_equal(error.reason, "repository_not_absolute",
                      f"{getarnt!r} wurde mit falschem Grund abgelehnt")

    # Ein ECHTES `~/…` bleibt selbstverstaendlich eine Zielangabe.
    require(os.path.isabs(os.path.expanduser("~/x")), "expanduser tut hier nichts")


def t_the_answer_does_not_depend_on_the_working_directory():
    """Was geklont wird, darf nicht davon abhaengen, wo der Dienst gestartet wurde.

    Der cwd steht in keinem Vertrag. Er stand aber im Ergebnis — zweimal: erst
    ueber blosse Woerter, dann ueber getarnte Tilden. Dieser Test faehrt jede
    Eingabe aus zwei verschiedenen Arbeitsverzeichnissen und verlangt dasselbe
    Ergebnis.
    """
    source = _source_repo()
    manager = _manager(source)
    eingaben = ["Projekt", "", "docs", "docs/x", "../etc", "~x/../docs", "~x/..",
                "/etc", source, os.path.join(source, "unterordner")]
    os.makedirs(os.path.join(source, "unterordner"), exist_ok=True)

    def ergebnisse(von: str) -> list[str]:
        alt = os.getcwd()
        os.chdir(von)
        try:
            aus = []
            for wert in eingaben:
                try:
                    aus.append("OK:" + manager.resolve_repo(wert))
                except W.WorkspaceError as exc:
                    aus.append("NEIN:" + exc.reason)
            return aus
        finally:
            os.chdir(alt)

    anderswo = tempfile.mkdtemp(prefix="solvio-cwd-")
    require_equal(ergebnisse(source), ergebnisse(anderswo),
                  "dasselbe Ziel wurde je nach Arbeitsverzeichnis anders aufgeloest")


def t_a_sibling_that_merely_starts_with_the_allowed_name_is_refused():
    """Der klassische Praefix-Verwechslungsfehler, hier festgenagelt.

    Steht in der Allowlist `/pfad/repo`, dann ist `/pfad/repo-boese` ein voellig
    anderes Verzeichnis — aber ein `startswith` ohne Trennzeichen haelt es fuer
    ein Unterverzeichnis. Der Code macht es richtig (`allowed + os.sep`); dieser
    Test ist der Grund, warum es richtig bleibt.

    Gefunden durch eine Mutation, die sonst ueberlebt haette.
    """
    source = _source_repo()
    sibling = source + "-boese"
    os.makedirs(sibling, exist_ok=True)
    manager = _manager(source)

    error = require_raises(W.WorkspaceError, manager.resolve_repo, sibling,
                           message="ein Nachbarverzeichnis galt als Unterverzeichnis")
    require_equal(error.reason, "repository_not_allowed", "falscher Ablehnungsgrund")

    # Ein echtes Unterverzeichnis bleibt selbstverstaendlich erlaubt.
    inner = os.path.join(source, "unterordner")
    os.makedirs(inner, exist_ok=True)
    require_equal(manager.resolve_repo(inner), inner,
                  "ein echtes Unterverzeichnis wurde abgelehnt")


def t_the_form_rule_leaves_the_allowlist_the_only_gate():
    """Die Formregel darf keinen dritten Ort erschliessen.

    Was auch immer ein Modell hineinschreibt: entweder es landet beim Standard,
    oder es wird abgelehnt. Ein Ergebnis ausserhalb der Allowlist gibt es nicht
    — auch nicht ueber einen absoluten Pfad, der ja weiterhin erlaubt ist.
    """
    manager = _manager(_source_repo())
    erlaubt = manager._allowed
    versuche = ["Projekt", "", "/etc", "/", "~", "/tmp", "../..",
                "/Users", "//etc", "/etc/../etc", "~root", "$HOME"]
    for wert in versuche:
        try:
            ziel = manager.resolve_repo(wert)
        except W.WorkspaceError:
            continue                          # Ablehnung ist ein gutes Ende
        require(any(ziel == a or ziel.startswith(a + os.sep) for a in erlaubt),
                f"{wert!r} fuehrte nach {ziel} — ausserhalb der Allowlist")


def t_a_second_clone_for_the_same_run_is_refused():
    """Zwei Schreiber im selben Bereich sind die Art Kollision, die niemand
    zurueckverfolgt."""
    source = _source_repo()
    manager = _manager(source)
    manager.clone("ar-once01", source)
    require_raises(W.WorkspaceError, manager.clone, "ar-once01", source,
                   message="ein zweiter Klon fuer denselben Lauf entstand")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
