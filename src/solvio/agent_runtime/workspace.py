"""Arbeitsbereiche: ein Klon je Lauf, nie der Produktivbaum — und eine Ernte,
die kredentialtragenden Inhalt verweigert.

## Warum Klon und nicht Worktree

Menschen arbeiten hier mit Worktrees. Ein sandkastiger Builder darf keinen:

1. Ein Worktree teilt `.git` mit dem Produktivrepo — der Builder braeuchte
   Schreibrecht auf `/Users/solvio/solvio-core/.git`. Genau das darf er nie
   haben, und das Seatbelt-Profil versiegelt es.
2. Ein Klon enthaelt nur **versionierte** Dateien. `.env` ist unversioniert und
   damit strukturell nicht im Arbeitsbereich — dieselbe Lehre, aus der die
   Briefing-Mappe entstand, diesmal ohne Verzicht auf den Quelltext.

`--no-hardlinks` und ausdruecklich kein `--shared`/`--reference`: ein
Alternates-Klon zeigt in `.git/objects` des Produktivrepos hinein (unbenutzbar
unter dem Siegel oder das Siegel gebrochen), und ein Hardlink-Klon teilt Inodes
— ein Schreiber, der eine geteilte Objektdatei anfasst, beschaedigt das
Produktivrepo in place.

## Warum die Ernte der Ort der Kredentialgrenze ist

Gemessen (2026-08-29): der native Codex-Sandkasten verhindert NICHT, dass ein
modellgesteuertes Kommando `~/.codex/auth.json` liest **und in den
Arbeitsbereich kopiert**. Der Sandkasten kann das strukturell nicht verhindern
— Schreiben im Arbeitsbereich ist sein Zweck.

Also wird der Kanal dort geschlossen, wo Inhalt den Arbeitsbereich VERLAESST:
hier. Die Ernte prueft, was sie mitnimmt, und verweigert bei Anmeldegestalt —
sie bereinigt nicht. Ein halb geernteter Zweig waere schlimmer als keiner: er
saehe aus wie ein Ergebnis.

Das Zielrepo bleibt dabei **unberuehrt**. Ein Fetch in dessen Refs wuerde bei
jedem Lauf den Ref-Digest der Sicherung kippen und das komplette git-Buendel
(gemessen 55 von 67 MB je Satz) neu erzwingen, und `refs/agents/*` liefen dort
unbegrenzt auf.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from solvio.logging_setup import get_logger
from solvio import git_binary as _GB

log = get_logger("agent_runtime")

#: Frueher stand hier der Pfad `/usr/bin/git`. Das ist auf macOS nicht git,
#: sondern der xcselect-Weiterleiter — die eine Binary, die den GUI-Installer
#: oeffnet, wenn sie das Entwicklerverzeichnis nicht lesen darf (P0.2). Der
#: Pfad wird jetzt aufgeloest und geprueft, statt geraten.
def GIT() -> str:
    """Die gepruefte git-Binary. Eine Funktion, weil die Pruefung erst zur
    Laufzeit etwas messen kann — und weil ein Konstantenwert genau die Art
    Vermutung war, die hier fuenf GUI-Dialoge gekostet hat."""
    return _GB.resolve()

#: Wohin geerntet wird. Ein Core-eigenes bare-Repo, nicht das Zielrepo.
HARVEST_DIRNAME = "agent_harvest.git"
WORKSPACE_DIRNAME = "agent_workspaces"

#: Zielrepos kommen aus einer Core-eigenen Allowlist, realpath-geprueft.
DEFAULT_ALLOWED_REPOS = (os.path.expanduser("~/solvio-core"),)

#: Dateinamen, die nie in eine Ernte gehoeren — egal was drinsteht. Der Name
#: allein ist der Befund: wer `auth.json` committet, hat nichts Gutes vor.
FORBIDDEN_BASENAMES = frozenset({
    "auth.json", ".env", ".netrc", ".pgpass", "credentials", "credentials.json",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".htpasswd",
    "keychain-db", "login.keychain-db", ".claude.json",
})

#: Pfadteile, deren blosse Anwesenheit den Zweig disqualifiziert.
FORBIDDEN_PARTS = frozenset({".codex", ".ssh", ".solvio", ".solvio-vault",
                             ".solvio-approvals", "keychains"})

#: Wie viel Inhalt die Ernte prueft. Ein Deckel, weil ein bosartiger Zweig sonst
#: die Pruefung selbst zum Angriff machen koennte (eine Datei von 4 GB).
MAX_SCANNED_BYTES = 2_000_000
MAX_SCANNED_FILES = 4_000
MAX_BLOB_BYTES = 512_000


class WorkspaceError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NothingToHarvest(WorkspaceError):
    """Der Zweig steht noch auf dem Klonpunkt — es gibt kein Ergebnis.

    Kein Sonderfall von „abgelehnt": abgelehnt heisst, es lag etwas da, das
    nicht hinaus darf. Hier lag nichts da. Der Unterschied gehoert dem Nutzer,
    nicht dem Log.
    """


class HarvestRefused(WorkspaceError):
    """Der Zweig traegt Anmeldegestalt. Er wird NICHT geerntet, nicht bereinigt.

    Das ist die zweite Haelfte der Codex-Kredentialgrenze: Netz aus (gemessen)
    UND eine Ernte, die den Kopierweg schliesst.
    """


def state_dir() -> str:
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def harvest_path() -> str:
    return os.path.join(state_dir(), HARVEST_DIRNAME)


def workspace_root(run_id: str = "") -> str:
    base = os.path.join(state_dir(), WORKSPACE_DIRNAME)
    return os.path.join(base, run_id) if run_id else base


def allowed_repos() -> tuple[str, ...]:
    raw = (os.environ.get("SOLVIO_AGENT_REPOS", "") or "").strip()
    entries = [p for p in raw.split(":") if p] if raw else list(DEFAULT_ALLOWED_REPOS)
    return tuple(os.path.realpath(os.path.expanduser(p)) for p in entries)


def _git(*args: str, cwd: str = "", check: bool = True,
         timeout: float = 300.0) -> subprocess.CompletedProcess:
    """Ein festes Programm, eine Argumentliste, keine Shell.

    `env` ist ausdruecklich eng: git liest sonst `~/.gitconfig`, und eine
    `[url]`-Umschreibung oder ein `core.fsmonitor`-Hook dort waere eine Stelle,
    an der fremde Konfiguration in einen Sicherheitspfad hineinredet.
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.join(state_dir(), "githome"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_ASKPASS": "/usr/bin/false", "LANG": "C.UTF-8"}
    os.makedirs(env["HOME"], mode=0o700, exist_ok=True)
    # Ein Builder darf seine lokale Git-Konfiguration veraendern. Der Core
    # ignoriert Hooks, fsmonitor und Objekt-Ersetzungen. Vor Restore/Ernte
    # prueft _assert_local_config weitere lokale Git-Ausfuehrungswege.
    env["GIT_GRAFT_FILE"] = os.devnull
    result = subprocess.run([GIT(), "--no-replace-objects",
                             "-c", "core.hooksPath=" + os.devnull,
                             "-c", "core.fsmonitor=false",
                             "-c", "protocol.ext.allow=never", *args], cwd=cwd or None, env=env,
                            capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise WorkspaceError("git_failed", f"{args[0]}:{result.stderr.strip()[:200]}")
    return result


@dataclass
class Workspace:
    run_id: str
    path: str
    repo: str
    branch: str
    #: Der Commit, von dem der Agentenzweig ausging. Alles daran hing schon
    #: vorher im Repository und ist NICHT das Werk des Agenten.
    base: str = ""


class WorkspaceManager:
    """Legt Klone an, erntet Ergebnisse und raeumt auf."""

    def __init__(self, *, allowed: tuple[str, ...] | None = None) -> None:
        self._allowed = allowed if allowed is not None else allowed_repos()

    def configured_repositories(self) -> tuple[str, ...]:
        """Read-only display; resolve_repo still validates each actual use."""
        return tuple(self._allowed)

    # -- Klon ----------------------------------------------------------

    def resolve_repo(self, repo: str) -> str:
        """Realpath-geprueft gegen die Allowlist — `..` und Symlinks aufgeloest.

        Uebernommen aus dem stillgelegten Codex-Agenten: das war seine eine
        Zusicherung, die es wert war, weiterzuleben.

        **Was hier ankommt, ist modellabgeleitet.** Live gefunden: der
        gesprochene Auftrag „lege im Projekt eine Datei an" trug das blosse Wort
        `Projekt` als `target_repo` ins Buch. Daran zerbrach jeder Bauauftrag —
        und der schlechtere Teil war, wohin so ein Wert zeigt: `realpath` loest
        einen relativen Namen gegen das ARBEITSVERZEICHNIS des Dienstes auf, und
        das liegt im Produktivcheckout. Ein Wort wie `docs` haette damit in die
        Allowlist gezeigt, und der Vertrag haenge am cwd — der nirgends
        vereinbart ist.

        Entschieden wird deshalb daran, ob der Wert nach `expanduser` ABSOLUT
        ist — nicht daran, wie er aussieht:

        * absolut nach `expanduser` (`/…`, ein aufloesbares `~/…`) — eine echte
          Zielangabe. Streng geprueft, abgelehnt wenn ausserhalb der Allowlist.
        * relativ und mit `/` — als Zielangabe gemeint, ohne cwd nicht
          entscheidbar. Wird ABGELEHNT statt geraten.
        * relativ ohne `/` — Umgangssprache. Gilt als *nichts angegeben*, es
          greift der Standard.

        **Ein erster Anlauf pruefte die Zeichenform (`startswith("~")`), und das
        war zu wenig.** Ein adversarischer Test fand es: `~x/../docs` sieht aus
        wie ein Home-Pfad, ist aber keiner — `expanduser` laesst es unveraendert
        stehen, weil kein Benutzer `x` existiert, und `realpath` loeste es dann
        doch wieder gegen den cwd auf. Gemessen aus dem Produktiv-cwd des
        Dienstes ergab das `…/solvio-core/docs`, aus `/private/tmp` dagegen eine
        Ablehnung: derselbe Wert, verschiedene Ergebnisse. Damit war genau der
        dritte Ort erreichbar, den diese Regel ausschliessen soll — bis hin zum
        `.git` des Produktivbaums und zu fremden Worktrees.

        Die Allowlist hielt in allen gemessenen Faellen; sie war nie umgangen.
        Falsch war die Zusage darueber, was ueberhaupt bei ihr ankommt.

        Die Allowlist entscheidet weiterhin allein, was geklont werden darf. Ein
        freier Text kann kein Repository erschliessen: er landet entweder beim
        Standard oder in einer Ablehnung, nie bei einem dritten Ort.
        """
        raw = (repo or "").strip()
        if raw:
            # `expanduser` ZUERST, dann die Frage. `~x/…` kommt hier unveraendert
            # heraus und ist damit das, was es ist: ein relativer Pfad.
            expanded = os.path.expanduser(raw)
            if os.path.isabs(expanded):
                raw = expanded
            elif "/" in raw:
                # Der Wert selbst bleibt draussen — er ist modellabgeleitet.
                raise WorkspaceError("repository_not_absolute")
            else:
                log.info("agent_runtime.repo_hint_ignored", form="bare_word")
                raw = ""
        candidate = raw or (self._allowed[0] if self._allowed else "")
        if not candidate:
            raise WorkspaceError("no_repository")
        resolved = os.path.realpath(candidate)
        if not os.path.isdir(resolved):
            raise WorkspaceError("repository_missing", resolved)
        for allowed in self._allowed:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved
        raise WorkspaceError("repository_not_allowed", resolved)

    def clone(self, run_id: str, repo: str, *, slug: str = "arbeit") -> Workspace:
        source = self.resolve_repo(repo)
        target = os.path.join(workspace_root(run_id), "repo")
        if os.path.exists(target):
            raise WorkspaceError("workspace_exists", target)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        branch = f"agent/{slug}-{run_id}"
        # `--no-hardlinks` und KEIN `--shared`/`--reference`: siehe Modul-Docstring.
        _git("clone", "--no-hardlinks", "--quiet", source, target)
        base = _git("rev-parse", "HEAD", cwd=target).stdout.strip()
        _git("checkout", "-q", "-b", branch, cwd=target)
        # Der Klon soll den Produktivbaum auch nicht als Fernziel behalten:
        # ein `git push origin` von innen waere sonst eine Produktivmutation.
        _git("remote", "remove", "origin", cwd=target, check=False)
        self.assert_isolated(target, source)
        os.chmod(workspace_root(run_id), 0o700)
        log.info("agent_runtime.workspace_created", run_id=run_id, branch=branch)
        return Workspace(run_id=run_id, path=target, repo=source, branch=branch,
                         base=base)

    def assert_isolated(self, clone: str, source: str) -> None:
        """Weder Alternates noch geteilte Inodes. Beides waere ein Weg zurueck
        in das Produktivrepo — einer lesend, einer zerstoerend."""
        alternates = os.path.join(clone, ".git", "objects", "info", "alternates")
        if os.path.exists(alternates):
            raise WorkspaceError("clone_has_alternates", alternates)
        objects = os.path.join(clone, ".git", "objects")
        shared = []
        for base, _dirs, files in os.walk(objects):
            for name in files:
                path = os.path.join(base, name)
                with contextlib.suppress(OSError):
                    if os.stat(path).st_nlink > 1:
                        shared.append(path)
                if len(shared) > 3:
                    break
            if shared:
                break
        if shared:
            raise WorkspaceError("clone_shares_inodes", shared[0])

    def restore(self, *, run_id: str, path: str, repo: str, branch: str,
                base: str, requested_repo: str = "") -> Workspace:
        """Bestehenden Klon verifizieren, niemals Base oder Bindung erraten."""
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or "")
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base or "")
                or not branch.startswith("agent/") or not branch.endswith("-" + run_id)):
            raise WorkspaceError("workspace_binding_invalid")
        source = self.resolve_repo(repo)
        # Eine explizite Repositoryangabe muss weiterhin dieselbe sein. Ein
        # damaliger Default bleibt an das bereits ausgewaehlte Repo gebunden.
        requested = os.path.expanduser((requested_repo or "").strip())
        if requested and (os.path.isabs(requested) or "/" in requested):
            if self.resolve_repo(requested) != source:
                raise WorkspaceError("workspace_repository_changed")
        self._assert_local_workspace(path, run_id)
        self.assert_isolated(path, source)
        if _git("remote", cwd=path).stdout.strip():
            raise WorkspaceError("workspace_remote_added")
        if _git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip() != branch:
            raise WorkspaceError("workspace_branch_changed")
        commit = _git("cat-file", "-t", base, cwd=path, check=False)
        ancestor = _git("merge-base", "--is-ancestor", base, "HEAD", cwd=path, check=False)
        if commit.returncode or commit.stdout.strip() != "commit" or ancestor.returncode:
            raise WorkspaceError("workspace_base_changed")
        return Workspace(run_id=run_id, path=path, repo=source, branch=branch, base=base)

    def _assert_local_workspace(self, path: str, run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
            raise WorkspaceError("workspace_binding_invalid")
        expected = os.path.join(os.path.realpath(workspace_root()), run_id, "repo")
        nominal = os.path.abspath(os.path.join(workspace_root(run_id), "repo"))
        if (not os.path.isabs(path) or os.path.abspath(path) != nominal
                or os.path.realpath(path) != expected or not os.path.isdir(path)):
            raise WorkspaceError("workspace_path_changed")
        gitdir = os.path.join(path, ".git")
        if not os.path.isdir(gitdir) or os.path.islink(gitdir):
            raise WorkspaceError("workspace_git_not_local")
        for folder, dirs, files in os.walk(gitdir):
            if any(os.path.islink(os.path.join(folder, name)) for name in dirs + files):
                raise WorkspaceError("workspace_git_symlink")
        self._assert_local_config(path)
        common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path).stdout.strip()
        if os.path.realpath(common) != os.path.realpath(gitdir):
            raise WorkspaceError("workspace_git_not_local")
        top = _git("rev-parse", "--show-toplevel", cwd=path).stdout.strip()
        if os.path.realpath(top) != os.path.realpath(path):
            raise WorkspaceError("workspace_path_changed")

    def _assert_local_config(self, path: str) -> None:
        """Only inert clone metadata may affect Core Git operations.

        Read names without following includes, before status/add or any
        operation that can invoke filters, signing tools or a different tree.
        A task needing unusual Git configuration stays isolated for review.
        """
        config = os.path.join(path, ".git", "config")
        if not os.path.isfile(config) or os.path.islink(config):
            raise WorkspaceError("workspace_git_not_local")
        out = _git("config", "--no-includes", "--file", config,
                   "--null", "--name-only", "--list", cwd=path)
        allowed = {"core.repositoryformatversion", "core.filemode", "core.bare",
                   "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
                   "user.name", "user.email"}
        if any(key.lower() not in allowed for key in out.stdout.split("\0") if key):
            raise WorkspaceError("workspace_git_config_untrusted")

    # -- Die Kredentialgrenze der Ernte ---------------------------------

    def scan_branch(self, clone: str, branch: str, base: str = "") -> list[str]:
        """Was der Agent an diesem Zweig HINZUGEFUEGT hat und nach Anmeldung
        aussieht. Leer heisst sauber.

        **Geprueft werden die AENDERUNGEN, nicht der ganze Baum.** Live gelernt,
        und der Fehler war teuer: ein Scan ueber das gesamte Repository fand in
        139 Dateien „Kredentialgestalt" — `.env.example`, das Schuldenregister,
        die ADRs, die Module dieser Laufzeit. Alles Text, der ueber Schluessel
        SCHREIBT. Die Ernte haette damit immer verweigert, und eine Pruefung,
        die immer verweigert, schuetzt nichts; sie wird abgeschaltet.

        Die Frage ist auch gar nicht „enthaelt dieses Repository irgendwo das
        Wort token", sondern „hat der Agent Anmeldematerial hineingelegt". Was
        am Ausgangs-Commit hing, war vorher da und ist nicht sein Werk.

        Zwei Fragen zu jeder geaenderten Datei, beide noetig:

        * **Der Name.** `auth.json`, `.env`, ein Schluesselpaar — der Name allein
          ist der Befund; wer so etwas committet, hat nichts Gutes vor.
        * **Der Inhalt.** Ein umbenanntes `auth.json` heisst anders und ist
          dasselbe. Dafuer laeuft der Inhalt durch dieselben Muster wie das
          Ledger und den auth.json-eigenen Filter des Starters.
        """
        from solvio.agent_runtime.specialists import redact_specialist_output

        findings: list[str] = []
        if base:
            # Nur Hinzugefuegtes und Geaendertes. Geloeschtes kann nichts tragen.
            changed = _git("diff", "--name-only", "--diff-filter=AM",
                           f"{base}..{branch}", cwd=clone, check=False)
            paths = [line.strip() for line in changed.stdout.splitlines()
                     if line.strip()]
            if not paths:
                return []
            listing = _git("ls-tree", "-l", "--full-name", branch, "--", *paths,
                           cwd=clone)
        else:
            # Ohne bekannten Ausgangspunkt bleibt nur der ganze Baum — und das
            # ist ausdruecklich die schlechtere Lage, nicht die normale.
            listing = _git("ls-tree", "-r", "-l", "--full-name", branch, cwd=clone)
        scanned_bytes = 0
        scanned_files = 0
        for line in listing.stdout.splitlines():
            if not line.strip():
                continue
            try:
                meta, path = line.split("\t", 1)
                parts = meta.split()
                blob, size_raw = parts[2], parts[3]
            except (ValueError, IndexError):
                continue
            lowered = path.lower()
            base = os.path.basename(lowered)
            if base in FORBIDDEN_BASENAMES:
                findings.append(f"name:{path}")
                continue
            if any(part in FORBIDDEN_PARTS for part in lowered.split("/")):
                findings.append(f"path:{path}")
                continue
            scanned_files += 1
            if scanned_files > MAX_SCANNED_FILES or scanned_bytes > MAX_SCANNED_BYTES:
                # Ehrlich gedeckelt: was darueber liegt, wird NICHT als sauber
                # gemeldet, sondern als ungeprueft — und ungeprueft erntet nicht.
                findings.append("uncapped:tree_too_large")
                break
            try:
                size = int(size_raw)
            except ValueError:
                size = 0
            if size > MAX_BLOB_BYTES:
                continue
            scanned_bytes += size
            content = _git("cat-file", "blob", blob, cwd=self._clone_of(clone),
                           check=False)
            body = content.stdout
            if not body:
                continue
            # AUSDRUECKLICH nur die STRUKTURELLEN Muster, nicht das
            # kontextuelle Praedikat des Hauses (`is_credential`).
            #
            # Live gelernt: `is_credential` ist fuer AUSSAGEN gebaut — fuer
            # Prosa, in der jemand „mein Passwort ist X" schreibt. Ein
            # Quellbaum ist etwas anderes: dort steht das Wort `token` in jedem
            # zweiten Kommentar, und der Scan schlug an `.env.example`, am
            # Schuldenregister, an den ADRs und an den Modulen dieser Laufzeit
            # an — 139 Dateien. Eine Pruefung, die immer verweigert, schuetzt
            # nichts; sie wird abgeschaltet.
            #
            # Was echtes Anmeldematerial ausmacht, ist seine GESTALT: ein JWT,
            # ein `sk-`-Schluessel, ein `"refresh_token": "..."`-Feld. Genau
            # die faengt die Redaktion, und genau die faehrt in einer Ernte mit.
            if redact_specialist_output(body) != body:
                findings.append(f"content:{path}")
        return findings

    def _clone_of(self, clone: str) -> str:
        return clone

    # -- Ernte ----------------------------------------------------------

    def ensure_harvest_repo(self) -> str:
        path = harvest_path()
        if not os.path.isdir(os.path.join(path, "objects")):
            os.makedirs(path, mode=0o700, exist_ok=True)
            _git("init", "--bare", "--quiet", path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        return path

    def commit_pending(self, workspace: Workspace) -> bool:
        """Was der Builder hinterlassen hat, wird zu einem Commit.

        **Live gefunden, und es war der unehrlichste Fehler dieses Milestones.**
        Der Builder legte die Datei an und committete sie nicht. Die Ernte nahm
        den Zweig, wie er war — also den Klonpunkt —, meldete Erfolg, und SOLVIO
        sagte dem Nutzer „das Ergebnis liegt bereit". Es lag nichts bereit. Die
        Ernte-Ref zeigte auf denselben Commit, von dem der Klon ausging.

        Sich darauf zu verlassen, dass ein fremdes Werkzeug committet, ist genau
        die Annahme, die hier gebrochen ist. Die Ernte hat die Aufgabe, aus
        Arbeit ein Ergebnis zu machen — also macht sie es selbst.

        `git add -A` respektiert `.gitignore` des geklonten Repositoriums; was
        dort ausgeschlossen ist, wird nicht mitgenommen. Und der Commit passiert
        VOR dem Scan, nie danach: sonst pruefte der Scan an der Arbeit vorbei.
        """
        self.restore(run_id=workspace.run_id, path=workspace.path, repo=workspace.repo,
                     branch=workspace.branch, base=workspace.base)
        out = _git("status", "--porcelain", cwd=workspace.path, check=False)
        if out.returncode != 0 or not out.stdout.strip():
            return False
        _git("add", "-A", cwd=workspace.path)
        _git("-c", "user.name=SOLVIO Agent",
             "-c", f"user.email=agent+{workspace.run_id}@solvio.local",
             "commit", "-q", "--no-verify",
             "-m", f"Arbeitsergebnis des Laufs {workspace.run_id}",
             cwd=workspace.path)
        log.info("agent_runtime.workspace_committed", run_id=workspace.run_id)
        return True

    def harvest(self, workspace: Workspace) -> str:
        """Ref-only-Fetch in das Ernte-Repo — nach der Kredentialpruefung.

        Reihenfolge ist die ganze Zusage: erst festhalten, dann pruefen, dann
        holen. Wer erst holt und dann prueft, hat das Material schon.
        """
        self.commit_pending(workspace)

        # Eine Ernte, die auf den Klonpunkt zeigt, ist keine Ernte. Sie sah
        # live wie ein Erfolg aus und war eine leere Zusage — die Sicherung
        # steht hier, damit sie unabhaengig davon greift, ob oben etwas
        # festgehalten wurde.
        head = _git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
        if workspace.base and head == workspace.base:
            log.warning("agent_runtime.harvest_empty", run_id=workspace.run_id)
            raise NothingToHarvest("no_change", workspace.branch)

        findings = self.scan_branch(workspace.path, workspace.branch,
                                    base=workspace.base)
        if findings:
            log.warning("agent_runtime.harvest_refused", run_id=workspace.run_id,
                        count=len(findings), first=findings[0][:60])
            raise HarvestRefused("credential_shaped_content", findings[0][:120])

        harvest = self.ensure_harvest_repo()
        ref = f"refs/agents/{workspace.run_id}"
        # `transfer.fsckObjects` laesst einen beschaedigten oder praeparierten
        # Objektstrom scheitern, statt ihn in das Buch zu uebernehmen.
        _git("-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
             "fetch", "--no-tags", "--quiet", workspace.path,
             f"{workspace.branch}:{ref}", cwd=harvest)
        log.info("agent_runtime.harvested", run_id=workspace.run_id, ref=ref)
        return ref

    def harvest_refs(self) -> list[str]:
        path = harvest_path()
        if not os.path.isdir(path):
            return []
        out = _git("for-each-ref", "--format=%(refname)", "refs/agents",
                   cwd=path, check=False)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def prune_refs(self, keep: set[str]) -> list[str]:
        """Die Ernte-Refs folgen der Ledger-Retention."""
        removed = []
        path = harvest_path()
        for ref in self.harvest_refs():
            if ref.rsplit("/", 1)[-1] in keep:
                continue
            _git("update-ref", "-d", ref, cwd=path, check=False)
            removed.append(ref)
        return removed

    # -- Aufraeumen ------------------------------------------------------

    def is_clean(self, workspace_path: str) -> bool:
        try:
            run_id = os.path.basename(os.path.dirname(workspace_path))
            self._assert_local_workspace(workspace_path, run_id)
            out = _git("status", "--porcelain", cwd=workspace_path, check=False)
        except (WorkspaceError, OSError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0 and not out.stdout.strip()

    def cleanup(self, run_id: str, *, force: bool = False) -> str:
        """Ein nicht-sauberer Bereich wird GEMELDET, nicht still entsorgt.

        „Da lagen noch Aenderungen" ist eine Auskunft, die ein Mensch haben
        will — und ein Verzeichnis, das man wegwirft, weil es unbequem ist,
        nimmt sie ihm.
        """
        folder = workspace_root(run_id)
        repo = os.path.join(folder, "repo")
        if not os.path.isdir(folder):
            return "absent"
        if os.path.isdir(repo) and not force and not self.is_clean(repo):
            log.warning("agent_runtime.workspace_unclean", run_id=run_id)
            return "unclean"
        shutil.rmtree(folder, ignore_errors=True)
        return "removed"

    def discard_incomplete(self, run_id: str) -> bool:
        """Ein Bruchstueck wegraeumen, das KEINEM Schreiber gehoert.

        Das ist ausdruecklich nicht `cleanup`. Gemeint ist genau ein Fall: das
        Buch kennt fuer diesen Lauf keinen Arbeitsbereich, auf der Platte liegt
        aber einer. Dann ist er der Rest eines Klonversuchs, der unterwegs
        abbrach — niemand arbeitet darin, und niemand vermisst ihn.

        Live gefunden: so ein Rest liess `clone` mit `workspace_exists`
        scheitern, und weil der Lauf danach nicht enden konnte, drehte sich der
        Takt endlos an derselben Stelle.

        Die Sperre `workspace_exists` bleibt unangetastet — sie ist es, die zwei
        Schreiber auseinanderhaelt. Hier wird nur aufgeraeumt, was das Buch
        nicht kennt, und es wird gemeldet, wenn es geschah.
        """
        if not os.path.isdir(workspace_root(run_id)):
            return False
        log.warning("agent_runtime.workspace_orphan_discarded", run_id=run_id)
        return self.cleanup(run_id, force=True) == "removed"

    def reconcile(self, ledger) -> list[str]:
        """Verwaiste Bereiche terminaler Laeufe entfernen, nicht saubere melden."""
        root = workspace_root()
        if not os.path.isdir(root):
            return []
        stale: list[str] = []
        for run_id in sorted(os.listdir(root)):
            run = ledger.get_run(run_id)
            if run is None or run.terminal:
                if self.cleanup(run_id) != "removed":
                    stale.append(run_id)
        return stale
