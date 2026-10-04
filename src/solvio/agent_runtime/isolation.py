"""Die Grenze eines schreibenden Spezialisten ist ein Betriebssystem-Sandkasten,
kein Politik-Flag.

Gemessen, nicht angenommen: Codex bringt seinen eigenen OS-Sandkasten mit
(`--sandbox workspace-write`). **Claude Code hat keinen.** `--permission-mode`
und Werkzeuglisten sind Politik — und ein Builder, der Tests ausfuehrt, fuehrt
beliebigen Code aus. Fuer den Berater hat die Briefing-Architektur schon
festgehalten, dass „seine Gutmuetigkeit" als Grenze nicht akzeptabel ist; fuer
einen schreibenden, testausfuehrenden Agenten gilt das erst recht. DEBT-0128
war genau diese Gestalt: unsandkastiger Unterprozess mit Schluesselreichweite.

Also bekommt der Claude-Builder ein SOLVIO-eigenes Seatbelt-Profil, nach dem
zweifach erprobten Muster aus `deep/isolation.py` und `bots/runner.py`.

Drei Dinge daran sind Entscheidungen und keine Vorsicht:

**1. Ein Profil ist eine Erlaubnisliste.** `~/solvio-core`, `~/.solvio*`, der
Tresor, die Freigaben, `~/.codex` und `~/.ssh` stehen NICHT unter einem
`deny` — sie stehen gar nicht drin. Nichts unter `/Users` ist erlaubt ausser
dem Arbeitsbereich, dem Scratch und dem EIGENEN Werkzeugzustand des CLIs. Ein
`(subpath "{home}")` waere die eine Zeile, die alles davon auf einmal
aufmachte; ein Test verlangt, dass sie fehlt.

**2. Kein Rueckschleifen-Ziel.** Der Kaefig von Hermes darf genau einen
Loopback-Port erreichen (den Broker). Ein Builder braucht keinen — die Zeile
fehlt hier, und damit sind Core (8766), Freigabeweg (8770) und Broker (8792)
nicht erreichbar. Die bekannte Seatbelt-Einschraenkung bleibt und wird wie in
`deep/isolation.py` benannt: das `*` in `(remote tcp "*:443")` ist das
**Wirtsfeld**, nicht der Port — `127.0.0.1:443` und `:80` sind also erreichbar.
Auf 443/80 bindet der Core nichts.

**3. Die Kredentialgrenze ist die ACL des Schluesselbund-Eintrags, nicht dieses
Profil.** Am laufenden System gemessen (2026-08-29): die wiederverwendbare
Claude-Sitzung liegt AUSSCHLIESSLICH im macOS-Schluesselbund (Eintrag
`Claude Code-credentials` in `login.keychain-db`); `~/.claude.json` traegt unter
`oauthAccount` nur Konto-Metadaten (Schluesselinventar geprueft: keine
Token-Felder), und `~/.claude/` haelt Verlauf, Sitzungen und Einstellungen. Der
lesbare `~/.claude*`-Zustand ist damit kredentialfrei — deshalb darf er im
Profil stehen. Das CLI selbst muss sich anmelden koennen und braucht dafuer
`mach-lookup`; die Grenze zu seinen Werkzeug-Kindern ist die **binaergebundene
ACL** des Eintrags: ein `bash`-Kind ist ein anderes Binary. Das ist ein
BINAERES B2-Gate — gelingt einem Kind der Lesezugriff still, ist der
Claude-Builder BLOCKIERT und der Codex-only-Rueckfall greift. Es wird nicht
weggeredet, und das Gate wird nicht aufgeweicht, damit es besteht.

Der `(deny process-exec)` auf `/usr/bin/security` weiter unten ist
ausdruecklich **zusaetzlich** und nicht der Mechanismus: er macht die Absicht
lesbar und kostet nichts. Wer ihn fuer die Grenze haelt, hat die ACL nicht
verstanden.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import stat
import time
from dataclasses import dataclass

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

PROFILE_NAME = "builder.sandbox.sb"

#: Werkzeugketten, die ein Builder ausfuehren darf. Bewusst als Liste im Code —
#: ein Suchpfad aus der Umgebung waere ein Weg, dem Builder ein untergeschobenes
#: Programm unterzujubeln (dieselbe Ueberlegung wie bei `STANDARD_BINARIES`).
TOOLCHAIN_ROOTS = ("/usr", "/bin", "/opt/homebrew", "/usr/local")


def trusted_git_root() -> str:
    """Die Wurzel der gepruefte git-Binary — oder leer, wenn es keine gibt.

    Ohne sie ist der Kaefig fuer git blind: `/usr/bin/git` ist auf macOS der
    xcselect-Weiterleiter, und wenn er das Entwicklerverzeichnis nicht lesen
    darf, haelt er die Werkzeuge fuer nicht installiert und oeffnet den
    GUI-Installer. Gemessen am 2026-09-02: neun Kernel-Verweigerungen auf
    `libxcrun.dylib` aus genau diesem Profil, und fuenf Installationen, die
    nichts geholfen haben, weil nie etwas gefehlt hat.

    Es ist keine Erweiterung der Rechte: `/usr` ist ohnehin lesbar und
    ausfuehrbar, der Weiterleiter also erreichbar. Diese Zeile macht nur den
    Weg gangbar, den er ohnehin nehmen wollte.
    """
    try:
        from solvio import git_binary as _GB
        return toolchain_root(_GB.resolve())
    except Exception:                                    # noqa: BLE001
        return ""

#: Pfade, die im gerenderten Profil NICHT vorkommen duerfen. Sie stehen hier
#: nur, damit ein Test ihre Abwesenheit pruefen kann — das Profil selbst ist
#: eine Erlaubnisliste und nennt sie nie.
SEALED_PATHS = (
    "~/solvio-core",
    "~/.solvio",
    "~/.solvio-vault",
    "~/.solvio-portal",
    "~/.solvio-approvals",
    "~/.solvio-approvals-production",
    # N8/C4: das native Codex-Home des Nexus liegt hier — mit `auth.json`
    # (gemessen, 0600). Die Pfadgrenzen-Pruefung liess `~/.solvio-nexus/...`
    # bisher durch, weil nur `~/.solvio` und `~/.solvio/` versiegelt waren.
    "~/.solvio-nexus",
    "~/.codex",
    "~/.ssh",
    ".env",
)

#: N8/C4: Siegel NUR fuer das Worker-Profil (`worker/claude`). Nicht global:
#: das (gesperrte, aber getestete) Abo-Builder-Profil bindet `~/.claude` per
#: `claude_tool_state()` ein — ein globales Siegel faerbte diese Zusicherung
#: rot. Der Worker hat keinen Anspruch auf den Owner-CLI-Zustand: sein Zustand
#: liegt per `CLAUDE_CONFIG_DIR` im Kaefig.
WORKER_SEALED = ("~/.claude", "~/.claude.json")

#: N8/C4: die Wurzel des Task-Arbeitsraums und der Claude-Kaefige. Das Profil
#: darf ihre Task-BLAETTER nennen (`.../workspaces/<task_id>`), die Wurzeln
#: selbst aber nie — sonst saehe ein Task jeden Nachbarn. Die bestehende
#: Pfadgrenzen-Regel taugt hier nicht (sie traefe das eigene Blatt), deshalb
#: prueft `sealed_violations` diese Pfade EXAKT.
TASK_ROOT = "~/.solvio-tasks"
SEALED_ROOTS = (TASK_ROOT, TASK_ROOT + "/workspaces", TASK_ROOT + "/claude-jails")


def sealed_paths() -> tuple[str, ...]:
    """`SEALED_PATHS` plus das KONFIGURIERTE native Codex-Home, falls es
    woanders liegt als `~/.solvio-nexus`.

    Die Konfiguration wird gelesen, nie angenommen; ist sie nicht ladbar,
    bleibt die feste Liste — ein Siegel weniger ist hier kein Fehlerzustand,
    weil die feste Liste den gemessenen Ort bereits traegt.
    """
    extra: list[str] = []
    try:
        from solvio.config import load_settings
        home = str(getattr(load_settings(), "agent_runtime_hermes_codex_home", "") or "").strip()
    except Exception:                                    # noqa: BLE001
        home = ""
    if home:
        resolved = os.path.realpath(os.path.expanduser(home))
        known = {os.path.realpath(os.path.expanduser(p)) for p in SEALED_PATHS if p != ".env"}
        if not any(resolved == k or resolved.startswith(k + os.sep) for k in known):
            extra.append(home)
    return SEALED_PATHS + tuple(extra)


def sealed_roots(*roots: str) -> tuple[str, ...]:
    """Die exakt zu versiegelnden Wurzeln — die Vorgabe plus konfigurierte
    Wurzeln (Arbeitsraum, Kaefig) einer isolierten Welt."""
    return SEALED_ROOTS + tuple(r for r in roots if r)

#: Rueckschleifen-Ports, die aus dem Builder-Kaefig unerreichbar sein muessen.
FORBIDDEN_LOOPBACK_PORTS = (8766, 8770, 8791, 8792, 8123)


class BuilderJailUnavailable(RuntimeError):
    """Kein Sandkasten, kein Builder. Es gibt keinen Halbzustand."""


_PROFILE = """(version 1)
(deny default)

;; --- Prozess -----------------------------------------------------------
;; `process-exec` NUR auf der Werkzeugkette, ausdruecklich NICHT auf dem
;; Arbeitsbereich: Tests laufen als `python <skript>` — das braucht Lesen am
;; Skript und Ausfuehren am Interpreter, nicht Ausfuehren an der Datei. Damit
;; kann ein Builder kein Programm bauen und starten, das er selbst geschrieben
;; hat. Braucht ein Repo das wirklich, ist das eine eigene, aufgezeichnete
;; Entscheidung — kein stilles Aufweichen hier.
(allow process-fork)
(allow process-exec
{toolchain_exec})
(allow signal (target self))
(allow sysctl-read)
;; Das CLI muss sich bei seinem eigenen Anbieter anmelden koennen. Die Grenze zu
;; seinen Werkzeug-Kindern ist die binaergebundene ACL des Schluesselbund-
;; Eintrags, nicht diese Zeile — siehe Modul-Docstring.
(allow mach-lookup)
(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))

;; Zweites Schloss, ausdruecklich NICHT der Mechanismus: das Werkzeug, mit dem
;; man einen Schluesselbund-Eintrag von der Kommandozeile holt, ist hier nicht
;; ausfuehrbar. Der Mechanismus bleibt die ACL.
(deny process-exec (literal "/usr/bin/security"))

;; --- Pfadaufloesung; als Liste, nicht global (sonst Metadatenleck) ------
(allow file-read-metadata
  (literal "/") (literal "/etc") (literal "/var") (literal "/tmp")
  (subpath "/usr") (subpath "/System") (subpath "/bin") (subpath "/dev")
  (subpath "/private/etc") (subpath "/private/var/db") (subpath "/private/var/run")
{metadata_paths})

;; --- Lesen -------------------------------------------------------------
;; Nichts unter /Users ausser Arbeitsbereich, Scratch und dem EIGENEN
;; Werkzeugzustand des CLIs. Kein `{home}`-Subpath — das ist die eine Zeile,
;; die den Tresor, die Freigaben, `~/.codex`, `~/.ssh` und jede `.env` auf
;; einmal aufmachte.
(allow file-read*
  (literal "/")                          ;; dyld liest das Wurzelverzeichnis selbst
  (subpath "/usr") (subpath "/System") (subpath "/bin")
  (subpath "/private/var/db")
  (subpath "/private/etc/ssl")
  (literal "/private/etc/hosts") (literal "/private/etc/resolv.conf")
  ;; `/bin/sh` liest beim Start `/private/var/select/sh`. Ohne diese Zeile
  ;; scheitert jede Shell mit „Operation not permitted" — eine Meldung, die
  ;; nach einer Rechtefrage aussieht und eine Pfadfrage ist.
  (subpath "/private/var/select")
  (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty")
{read_paths})

;; --- Schreiben: Arbeitsbereich, Scratch, eigener Werkzeugzustand -------
(allow file-write*
{write_paths})
(allow file-write-data
  (literal "/dev/null") (literal "/dev/dtracehelper")
  (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty"))

;; --- Netz: HTTPS, HTTP, DNS. KEIN Rueckschleifen-Ziel. ----------------
;; Der Kaefig von Hermes darf genau einen Loopback-Port (den Broker). Ein
;; Builder braucht keinen — die Zeile fehlt hier. Zur Ehrlichkeit: das `*` in
;; `(remote tcp "*:443")` ist das WIRTSFELD, nicht der Port; 127.0.0.1:443 und
;; :80 sind damit erreichbar. Auf 443/80 bindet der Core nichts.
{network}
(allow system-socket)
"""

#: Der Netzblock des gewoehnlichen Builders: hinaus ins Netz, kein Loopback.
#: Codex braucht ihn nicht (sein eigener Sandkasten pinnt `network_access` auf
#: `false`), aber ein Builder, der Pakete zieht, braucht ihn.
_NETWORK_EGRESS = """(allow network-outbound
  (remote tcp "*:443")
  (remote tcp "*:80")
  (remote tcp "*:53")
  (remote udp "*:53")
  (path "/private/var/run/mDNSResponder"))"""

#: Der Netzblock des **gemakelten** Builders (Development Autopilot V0.6):
#: GENAU ein Ziel, die Rueckschleife zum eigenen Broker. Kein 443, kein 80,
#: kein DNS, kein mDNS.
#:
#: Gemessen am 2026-09-02 mit `claude --bare`: ein voller Turn kommt damit
#: durch. Ebenso gemessen, was NICHT geht — `api.anthropic.com` (kein DNS),
#: `claude.ai`, jeder andere Loopback-Port (auch der Core auf 8766), und
#: `/usr/bin/security` (exec verweigert, die Zeile steht ohnehin oben).
#:
#: **Syntaxfalle, teuer gelernt:** `sandbox-exec` weist `(remote tcp
#: "127.0.0.1:8792")` mit „host must be * or localhost" ab. Es muss
#: `localhost` heissen.
_NETWORK_BROKER_ONLY = """(allow network-outbound
  (remote tcp "localhost:{broker_port}"))"""

#: N8/C4: GENAU eine zusaetzliche Zeile je Werkzeugbruecken-Endpunkt — die
#: AF_UNIX-Form, wie `(path "/private/var/run/mDNSResponder")` im Egress-Block.
#: Gemessen am 2026-09-18 (M-C4): `sandbox-exec` nimmt `(path ...)` fuer den
#: Unix-Socket an; ohne die Zeile scheitert der `connect` mit
#: „Operation not permitted".
_NETWORK_UNIX_SOCKET = """(allow network-outbound
  (path "{endpoint}"))"""


def toolchain_root(program: str) -> str:
    """Das Verzeichnis, das ein Werkzeug wirklich braucht — mit der uv-Falle.

    Uebernommen aus `deep/isolation.interpreter_root`, weil es genau die Stelle
    ist, an der ein Profil STILL scheitert: `venv/bin/python` zeigt bei uv auf
    einen Alias ohne Patchnummer, waehrend `realpath` beim konkreten Bau landet.
    Erlaubt man nur den aufgeloesten Pfad, verweigert der Kern schon das
    `execvp` — mit einer Meldung, die nach einem Rechteproblem aussieht und ein
    Pfadproblem ist.
    """
    resolved = os.path.realpath(program)
    parts = resolved.split(os.sep)
    if "uv" in parts:
        index = parts.index("uv")
        if len(parts) > index + 1 and parts[index + 1] == "python":
            return os.sep.join(parts[:index + 2])
    return os.path.dirname(os.path.dirname(resolved))


def _subpaths(paths) -> str:
    return "\n".join(f'  (subpath "{os.path.realpath(os.path.expanduser(p))}")'
                     for p in paths if p)


def render_profile(*, workspace: str, scratch: str, tool_state: tuple[str, ...] = (),
                   toolchain: tuple[str, ...] = TOOLCHAIN_ROOTS,
                   broker_port: int = 0,
                   unix_sockets: tuple[str, ...] = ()) -> str:
    """Baut das Builder-Profil fuer genau diesen Arbeitsbereich.

    Alle Pfade laufen durch `realpath` — ein Symlink, der aus dem Arbeitsbereich
    heraus zeigt, wuerde sonst mitgenehmigt.

    `broker_port > 0` schaltet auf den **gemakelten** Netzblock um: genau die
    Rueckschleife zu diesem Port, sonst nichts. Das ist keine zweite
    Sandkasten-Architektur, sondern eine Zeile — alles andere am Profil bleibt
    Wort fuer Wort dasselbe, `/usr/bin/security` eingeschlossen.

    `unix_sockets` (N8/C4) nennt die Endpunkte der Core-Werkzeugbruecke: je
    Endpunkt GENAU eine AF_UNIX-Zeile, nur zusammen mit dem gemakelten
    Netzblock — ein Egress-Builder hat keine Werkzeugbruecke.
    """
    workspace = os.path.realpath(os.path.expanduser(workspace))
    scratch = os.path.realpath(os.path.expanduser(scratch))
    kette = tuple(toolchain)
    wurzel = trusted_git_root()
    if wurzel and wurzel not in kette:
        kette += (wurzel,)
    existing_toolchain = tuple(p for p in kette if os.path.isdir(p))
    owned = (workspace, scratch) + tuple(
        os.path.realpath(os.path.expanduser(p)) for p in tool_state)

    read = existing_toolchain + owned
    if broker_port:
        if not 1 <= int(broker_port) <= 65535:
            raise ValueError("broker port out of range")
        network = _NETWORK_BROKER_ONLY.format(broker_port=int(broker_port))
        for endpoint in unix_sockets:
            if (not endpoint or not os.path.isabs(endpoint) or '"' in endpoint
                    or "\n" in endpoint or os.path.realpath(endpoint) != endpoint):
                raise ValueError("unix socket endpoint invalid")
            network += "\n" + _NETWORK_UNIX_SOCKET.format(endpoint=endpoint)
    else:
        if unix_sockets:
            raise ValueError("unix sockets need the brokered network block")
        network = _NETWORK_EGRESS
    return _PROFILE.format(
        toolchain_exec=_subpaths(existing_toolchain),
        metadata_paths=_subpaths(read),
        read_paths=_subpaths(read),
        write_paths=_subpaths(owned),
        network=network,
        home=os.path.expanduser("~"))


def claude_tool_state() -> tuple[str, ...]:
    """Der eigene Zustand des Claude-CLIs — Verlauf, Sitzungen, Einstellungen.

    Gemessen kredentialfrei: die wiederverwendbare Sitzung liegt im
    Schluesselbund, `~/.claude.json` traegt nur Konto-Metadaten. Deshalb darf
    er gelesen und geschrieben werden; er ist Werkzeugzustand, keine Anmeldung.
    """
    home = os.path.expanduser("~")
    return tuple(p for p in (os.path.join(home, ".claude"),) if os.path.isdir(p))


def sealed_violations(profile: str, sealed: tuple[str, ...] | None = None,
                      roots: tuple[str, ...] | None = None) -> list[str]:
    """Welche versiegelten Pfade das gerenderte Profil doch nennt.

    Der Vergleich laeuft ueber `realpath`, weil das Profil aufgeloeste Pfade
    traegt — ein Test, der `~/.solvio` als Text sucht, faende nichts und waere
    still gruen.

    `sealed` (Vorgabe: `sealed_paths()`) sind die Pfade mit PFADGRENZEN-Regel:
    weder der Pfad noch etwas darunter darf genannt sein. `roots` (Vorgabe:
    `SEALED_ROOTS`) sind die Wurzeln mit EXAKTER Regel: die Wurzel selbst darf
    nicht genannt sein, ihre Task-Blaetter wohl. Die Signatur mit nur `profile`
    bleibt gueltig.
    """
    # Nur die REGELN, nicht die Erklaerungen. Das Profil sagt in einem
    # Kommentar ausdruecklich, dass `.env` und `~/.codex` nicht vorkommen
    # duerfen — eine Textsuche ueber die Rohfassung schlaegt also ausgerechnet
    # an der Zeile an, die es richtig macht, und erzieht dazu, weniger zu
    # erklaeren. Dieselbe Lehre wie beim Quellscan des Fachteams.
    rules = "\n".join(line for line in profile.splitlines()
                      if not line.lstrip().startswith(";"))
    hits = []
    home = os.path.expanduser("~")
    for sealed_path in (sealed_paths() if sealed is None else sealed):
        if sealed_path == ".env":
            if ".env" in rules:
                hits.append(sealed_path)
            continue
        resolved = os.path.realpath(os.path.expanduser(sealed_path))
        # `~/.solvio` ist ein Praefix von `~/.solvio-vault`: geprueft wird auf
        # Pfadgrenze, sonst meldet der Test den Nachbarn statt des Treffers.
        for line in rules.splitlines():
            if f'"{resolved}"' in line or f'"{resolved}/' in line:
                hits.append(sealed_path)
                break
    # Die Task-Wurzeln: exakt, nicht als Grenze — das eigene Blatt darf drin
    # stehen, die Wurzel (und damit jeder Nachbar) nie.
    for root in (SEALED_ROOTS if roots is None else roots):
        resolved = os.path.realpath(os.path.expanduser(root))
        if f'"{resolved}"' in rules or f'"{resolved}/"' in rules:
            hits.append("root:" + root)
    # Und die eine Zeile, die alles auf einmal aufmachte.
    if f'(subpath "{home}")' in rules:
        hits.append("~")
    return sorted(set(hits))


def worker_profile(*, workspace: str, jail: str, broker_port: int,
                   unix_sockets: tuple[str, ...] = (),
                   toolchain: tuple[str, ...] = TOOLCHAIN_ROOTS,
                   roots: tuple[str, ...] | None = None) -> str:
    """Das Profil des Claude-AUFTRAGSARBEITERS (N8/C4) — gerendert UND gesiegelt.

    Gegenueber dem gemakelten Builder (V0.6) drei Dinge: kein `~/.claude`
    (`tool_state=()`, der CLI-Zustand liegt per `CLAUDE_CONFIG_DIR` im Kaefig),
    die Worker-Siegel `WORKER_SEALED` zusaetzlich zu den globalen, und die
    exakte Wurzelregel fuer die Task-Wurzeln. Verletzt das Ergebnis ein Siegel,
    gibt es kein Profil — `BuilderJailUnavailable`, kein Halbzustand.
    """
    if not broker_port:
        raise BuilderJailUnavailable("worker profile needs the broker port")
    body = render_profile(workspace=workspace, scratch=jail, tool_state=(),
                          toolchain=toolchain, broker_port=broker_port,
                          unix_sockets=unix_sockets)
    violations = sealed_violations(body, sealed=sealed_paths() + WORKER_SEALED,
                                   roots=roots)
    if violations:
        raise BuilderJailUnavailable(f"profile names sealed paths: {violations}")
    return body


class JailEntryTampered(BuilderJailUnavailable):
    """Ein Core-geschriebener Name im Kaefig ist kein gewoehnlicher Eintrag mehr."""


def write_jail_file(path: str, data: bytes, *, mode: int = 0o600) -> str:
    """Ersetzt `<path>` durch eine regulaere Datei — nie durch einen Symlink hindurch.

    Der Kaefig ist zwischen zwei Turns fuer den Arbeiter beschreibbar (sein
    Profil erlaubt `file-write*` auf dem ganzen Scratch). Ein dort abgelegter
    Symlink unter einem Namen, den der Core schreibt (Profil, MCP-Konfiguration,
    Adapter), truege den Core-Schreibvorgang — ausserhalb jedes Sandkastens —
    an die Datei, die der Link nennt (Codex-Review 19.09.2026, Befund a). Darum:
    ein Symlink oder Nicht-Datei-Eintrag wird ABGEWIESEN, nicht repariert (ein
    manipulierter Kaefig ist ein Befund, kein Zustand zum Glaetten); ein
    regulaerer Eintrag wird entfernt und mit `O_CREAT|O_EXCL` neu angelegt,
    sodass zwischen Pruefung und Oeffnen nichts untergeschoben werden kann. Eine
    zweite Verknuepfung derselben Datei (`st_nlink > 1`) gilt ebenso als
    manipuliert. Der Sandkasten verbietet beides bereits nach aussen — diese
    Funktion ist die Schranke, die auch ohne ihn haelt.
    """
    parent, name = os.path.split(path)
    # The parent is opened without following a symlink and every step below goes
    # through that descriptor: `O_NOFOLLOW` alone guards only the last path
    # component (review round 11, H11-8).
    try:
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise JailEntryTampered(f"jail directory is not a directory: {os.path.basename(parent)}") from exc
    try:
        try:
            info = os.lstat(name, dir_fd=dfd)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise JailEntryTampered(f"jail entry is not a private regular file: {name}")
            os.unlink(name, dir_fd=dfd)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        except FileExistsError as exc:
            # Something appeared between unlink and create: a planted entry.
            raise JailEntryTampered(f"jail entry appeared during replacement: {name}") from exc
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return path


def make_jail_dir(path: str) -> str:
    """`mkdir -p` 0o700 — ein Symlink an dieser Stelle ist ein manipulierter Kaefig."""
    if os.path.islink(path):
        raise JailEntryTampered(f"jail directory is a symlink: {os.path.basename(path)}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise JailEntryTampered(f"jail directory is not a directory: {os.path.basename(path)}")
    return path


def install_profile(scratch: str, body: str) -> str:
    """Schreibt ein gesiegeltes Profil 0o600 in den Kaefig; liefert den Pfad.
    Nie durch einen Symlink hindurch (`write_jail_file`)."""
    make_jail_dir(scratch)
    return write_jail_file(os.path.join(scratch, PROFILE_NAME), body.encode("utf-8"))


@dataclass
class BuilderProcess:
    process: object
    profile_path: str
    pgid: int
    started_at: float
    executable: str


def available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


async def launch(argv: list[str], *, workspace: str, scratch: str,
                 env: dict[str, str], tool_state: tuple[str, ...] = (),
                 timeout: float = 900.0,
                 broker_port: int = 0) -> tuple[object, BuilderProcess]:
    """Startet ein Kind unter dem Builder-Profil. Fail-closed an jeder Stelle.

    Kein „notfalls eben ohne Sandkasten": fehlt `sandbox-exec`, gibt es keinen
    Builder. Ein Lauf, der dann bei `specialist_unavailable` stehen bleibt, ist
    die ehrlichere Lage als ein schreibender Agent ohne Kernel-Grenze.
    """
    if not available():
        raise BuilderJailUnavailable("sandbox-exec missing")
    if not argv or (shutil.which(argv[0]) is None and not os.path.exists(argv[0])):
        raise BuilderJailUnavailable("builder binary missing")

    body = render_profile(workspace=workspace, scratch=scratch,
                          tool_state=tool_state, broker_port=broker_port)
    violations = sealed_violations(body)
    if violations:
        # Kein `assert`: unter `python -O` waere die Pruefung weg — und das
        # Siegel genau dort offen, wo es am meisten schadet.
        raise BuilderJailUnavailable(f"profile names sealed paths: {violations}")
    profile_path = install_profile(scratch, body)

    process = await asyncio.create_subprocess_exec(
        SANDBOX_EXEC, "-f", profile_path, *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace, env=env, start_new_session=True)
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = 0
    log.info("agent_runtime.builder_launched", pid=process.pid, sandbox="seatbelt",
             egress="none" if broker_port else "443/80/53",
             loopback=str(broker_port) if broker_port else "none")
    return process, BuilderProcess(process=process, profile_path=profile_path,
                                   pgid=pgid, started_at=time.time(),
                                   executable=os.path.realpath(argv[0]))


def terminate(process) -> None:
    """Die ganze Prozessgruppe, nicht nur das Kind: ein CLI startet gern Helfer,
    und ein verwaister Helfer haelt Kontingent und Speicher fest."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
