"""Die eine vertrauenswuerdige git-Binary fuer unbeaufsichtigte Arbeit.

Gemessene Ursache (P0.2, 2026-09-02)
------------------------------------

`/usr/bin/git` ist auf macOS **kein git**. Es ist ein 118-KB-Weiterleiter, der
ueber `libxcselect` das aktive Entwicklerverzeichnis sucht und den Aufruf dorthin
reicht. Findet er es nicht, haelt er die Command Line Tools fuer nicht
installiert und startet den GUI-Installer.

Im Sandkasten DARF er es nicht finden. Der Kernel hat es protokolliert::

    Sandbox: git(26985) deny(1) file-read-metadata
             /Library/Developer/CommandLineTools/usr/lib/libxcrun.dylib

Der Weiterleiter kann die Verweigerung nicht von „fehlt" unterscheiden. Deshalb
kam der Dialog fuenfmal an einem Tag (08:02, 08:41, 17:35, 21:28, 22:48), und
deshalb half jede der fuenf erfolgreichen Installationen nichts: es fehlte nie
etwas. Eine sechste Installation haette wieder nichts geholfen.

Die Folgerung
-------------

Unbeaufsichtigte Arbeit ruft **nie** den Weiterleiter auf, sondern immer eine
Binary, die selbst git ist. Welche das ist, entscheidet nicht der PATH des
gerade laufenden Prozesses — ein launchd-Kontext hat ein karges PATH, und
`subprocess` faellt ohne PATH auf `/bin:/usr/bin` zurueck, also genau auf den
Weiterleiter.

Die Unterscheidung ist eine Byte-Signatur, kein Pfad und keine Vermutung: nur
der Weiterleiter traegt `libxcselect`. Auf Linux ist `/usr/bin/git` das echte
git und traegt sie nicht — dieselbe Regel entscheidet auf beiden Systemen
richtig, ohne nach dem Betriebssystem zu fragen.

`otool` waere der naheliegende Weg gewesen, die Abhaengigkeit zu lesen. Es ist
selbst ein Weiterleiter — die Pruefung haette genau den Dialog ausgeloest, den
sie verhindern soll.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess

#: Die Signatur des Weiterleiters.
SHIM_MARKER = b"libxcselect"

#: Wie weit in die Datei hinein gesucht wird. Der Weiterleiter ist 118 KB gross,
#: die echten Binaries sind Megabytes; die Signatur steht in den Ladebefehlen
#: am Anfang. Ein Deckel, damit die Pruefung nie teuer wird.
MAX_PROBE_BYTES = 1_000_000

#: Der Umgebungsname, mit dem der Betreiber eine Binary ausdruecklich setzt.
#: Sie muss dieselben Pruefungen bestehen wie jede andere — eine Angabe ist
#: eine Auswahl, keine Erlaubnis.
ENV_OVERRIDE = "SOLVIO_GIT"

#: In dieser Reihenfolge. Die beiden Apple-Pfade zuerst, weil sie auf diesem
#: Rechner die verifizierten sind; `/usr/bin/git` steht dabei, weil es auf Linux
#: das echte git ist — auf macOS faellt es durch die Signaturpruefung.
CANDIDATES: tuple[str, ...] = (
    "/Library/Developer/CommandLineTools/usr/bin/git",
    "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/usr/bin/git",
    "/bin/git",
)


class GitBinaryError(RuntimeError):
    """Es gibt keine vertrauenswuerdige git-Binary — laut und benannt.

    Das ist ausdruecklich besser als der bisherige Zustand: ein Fehler, den
    ein Log traegt, statt eines Dialogs, der auf einen Menschen wartet.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def is_forwarder(path: str) -> bool:
    """Traegt diese Datei die Signatur des xcselect-Weiterleiters?"""
    try:
        with open(path, "rb") as fh:
            return SHIM_MARKER in fh.read(MAX_PROBE_BYTES)
    except OSError:
        return False


def why_not(path: str, *, run: bool = True) -> str:
    """Leer, wenn die Binary vertrauenswuerdig ist — sonst der Grund.

    Die Reihenfolge ist Absicht: die Signaturpruefung steht VOR dem Probelauf.
    Ein Probelauf auf dem Weiterleiter waere genau der Aufruf, der den Dialog
    oeffnet — die Pruefung wuerde den Schaden anrichten, den sie sucht.
    """
    if not path or not os.path.isabs(path):
        return "not_absolute"
    try:
        st = os.stat(path)
    except OSError:
        return "absent"
    if not stat.S_ISREG(st.st_mode):
        return "not_a_file"
    if not os.access(path, os.X_OK):
        return "not_executable"
    if st.st_uid not in (0, os.geteuid()):
        return f"foreign_owner:{st.st_uid}"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "writable_by_others"
    try:
        eltern = os.stat(os.path.dirname(path))
        if eltern.st_mode & stat.S_IWOTH:
            return "directory_writable_by_others"
    except OSError:
        return "directory_unreadable"
    if is_forwarder(path):
        return "xcselect_forwarder"
    if not run:
        return ""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=20, check=False,
                              env={"LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unrunnable:{type(exc).__name__}"
    if proc.returncode != 0 or not proc.stdout.startswith("git version"):
        return f"not_git:{proc.stdout.strip()[:40] or proc.returncode}"
    return ""


def candidates(env: dict | None = None) -> list[str]:
    """Die Kandidaten in Pruefreihenfolge — Angabe zuerst, PATH zuletzt."""
    umgebung = os.environ if env is None else env
    liste = []
    gesetzt = (umgebung.get(ENV_OVERRIDE) or "").strip()
    if gesetzt:
        liste.append(gesetzt)
    liste += list(CANDIDATES)
    # Zuletzt und nur, falls die festen Pfade auf diesem System alle fehlen —
    # damit ein ungewoehnliches Betriebssystem nicht ohne git dasteht. Der
    # PATH entscheidet nie ALLEIN: was er nennt, muss dieselbe Pruefung
    # bestehen wie jeder feste Pfad.
    ueber_pfad = shutil.which("git", path=umgebung.get("PATH"))
    if ueber_pfad:
        liste.append(os.path.realpath(ueber_pfad))
    gesehen, geordnet = set(), []
    for p in liste:
        if p not in gesehen:
            gesehen.add(p)
            geordnet.append(p)
    return geordnet


_RESOLVED: str = ""
_REASONS: list[tuple[str, str]] = []


def resolve(*, env: dict | None = None, refresh: bool = False) -> str:
    """Der absolute Pfad der einen vertrauenswuerdigen git-Binary."""
    global _RESOLVED
    if _RESOLVED and not refresh:
        return _RESOLVED
    gruende: list[tuple[str, str]] = []
    for pfad in candidates(env):
        grund = why_not(pfad)
        gruende.append((pfad, grund or "ok"))
        if not grund:
            _RESOLVED = pfad
            _REASONS[:] = gruende
            return pfad
    _REASONS[:] = gruende
    raise GitBinaryError(
        "no_trusted_git",
        "; ".join(f"{p}={g}" for p, g in gruende)[:400])


def argv(*args: str, env: dict | None = None) -> list[str]:
    """`[<vertrauenswuerdiges git>, *args]` — die einzige Art, git zu rufen."""
    return [resolve(env=env), *args]


def report(*, env: dict | None = None) -> dict:
    """Was der Runbook und der Doctor darueber wissen wollen."""
    try:
        gewaehlt, fehler = resolve(env=env), ""
    except GitBinaryError as exc:
        gewaehlt, fehler = "", str(exc)
    return {"resolved": gewaehlt, "error": fehler,
            "checked": list(_REASONS),
            "forwarder_is_never_used": all(
                g != "ok" or not is_forwarder(p) for p, g in _REASONS)}
