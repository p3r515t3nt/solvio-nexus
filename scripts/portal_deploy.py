#!/usr/bin/env python3
"""Legt den Baum an, aus dem der Portal-Arbeiter laeuft — ganz oder gar nicht.

Der Arbeiter lebt hinter einer Benutzergrenze und laeuft aus einer Kopie. Zwei
Dinge muessen deshalb stimmen, und beide sind hier verankert:

**Vollstaendigkeit.** Ausgeliefert wird in ein neues Verzeichnis `app-<bau>`, das
erst nach der letzten Datei und nach der Sperrlistenpruefung sichtbar wird. Der
Umschalter ist ein Symlink, und ein Symlink laesst sich atomar ersetzen. Bricht
etwas ab, zeigt `app` unveraendert auf den bisherigen, vollstaendigen Baum. Der
Arbeiter fuehrt nie halb aktualisierten Code aus.

**Nachweisbarkeit.** Der Name des Verzeichnisses ist der Bauzustand. Der Arbeiter
nennt ihn beim Handschlag, der Core vergleicht ihn mit seinem eigenen Quellbaum,
und bei Abweichung beginnt keine Sitzung.

Was NICHT mitkommt, ist die interessantere Haelfte der Liste: `portal/vault.py`
(der Tresor bleibt beim Core), `portal/client.py`, `config.py`, `capabilities/`,
`security/`, `memory/`, `realtime/`, `integrations/`, `deep/`. Geprueft wird das
nach dem Kopieren — eine Sperrliste, die niemand kontrolliert, ist eine
Absichtserklaerung.

Laeuft als der Core-Nutzer. Kein `sudo`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from solvio.portal.build import FORBIDDEN, MODULES, compute_build  # noqa: E402

TARGET = "/Users/Shared/solvio-portal"
LINK = os.path.join(TARGET, "app")
VENV = os.path.join(TARGET, "venv")
PYTHON_DIR = os.path.join(TARGET, "python")
PYTHON_VERSION = "3.13"
REQUIREMENTS = ("aiohttp==3.14.3", "websockets==17.0.1", "structlog==26.1.0")

#: Wie viele fruehere Baustaende liegenbleiben. Einer genuegt zum Zurueckfallen;
#: ein Archiv aller je ausgelieferten Versionen ist ein Aufraeumproblem.
KEEP_BUILDS = 2


def run(argv: list[str]) -> None:
    print("  $", " ".join(argv))
    subprocess.run(argv, check=True, capture_output=True, text=True)


def interpreter() -> str:
    """Der eigene Interpreter im gemeinsamen Baum — nie der aus dem Zuhause."""
    for entry in sorted(os.listdir(PYTHON_DIR)):
        candidate = os.path.join(PYTHON_DIR, entry, "bin", "python3")
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("no interpreter installed under the shared tree")


def contamination(root: str) -> list[str]:
    found = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name in FORBIDDEN:
                found.append(os.path.join(base, name))
        for part in base.split(os.sep):
            if part in FORBIDDEN:
                found.append(base)
    return sorted(set(found))


def stage(build: str) -> str:
    """Kopiert in ein neues Verzeichnis. Sichtbar wird es erst beim Umschalten."""
    staged = os.path.join(TARGET, f"app-{build}")
    shutil.rmtree(staged, ignore_errors=True)
    for relative in MODULES:
        destination = os.path.join(staged, relative)
        os.makedirs(os.path.dirname(destination), mode=0o755, exist_ok=True)
        shutil.copy2(os.path.join(REPO, "src", relative), destination)
        os.chmod(destination, 0o644)
    return staged


def switch(staged: str) -> None:
    """Haengt den Symlink atomar um. `os.replace` ersetzt in einem Schritt.

    Einmalig eine Sonderbehandlung: die erste Auslieferung dieses Projekts hat
    `app` als echtes Verzeichnis angelegt, und ueber ein Verzeichnis laesst sich
    kein Symlink schieben. Es wird beiseitegeraeumt, nicht geloescht — falls
    danach etwas schiefgeht, liegt der alte Baum noch da.
    """
    if os.path.isdir(LINK) and not os.path.islink(LINK):
        legacy = LINK + "-legacy"
        shutil.rmtree(legacy, ignore_errors=True)
        os.rename(LINK, legacy)
        print(f"  Altbestand beiseitegeraeumt: {os.path.basename(legacy)}")
    temporary = LINK + ".new"
    if os.path.islink(temporary) or os.path.exists(temporary):
        if os.path.isdir(temporary) and not os.path.islink(temporary):
            shutil.rmtree(temporary)
        else:
            os.remove(temporary)
    os.symlink(staged, temporary)
    os.replace(temporary, LINK)


def prune(keep: str) -> None:
    builds = sorted((entry for entry in os.listdir(TARGET)
                     if entry.startswith("app-")), reverse=True)
    for entry in builds:
        path = os.path.join(TARGET, entry)
        if path == keep or builds.index(entry) < KEEP_BUILDS:
            continue
        shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    os.makedirs(TARGET, mode=0o755, exist_ok=True)
    build, _parts = compute_build(os.path.join(REPO, "src"))
    print(f"  Bauzustand: {build}")

    staged = stage(build)
    missing = [m for m in MODULES if not os.path.exists(os.path.join(staged, m))]
    if missing:
        shutil.rmtree(staged, ignore_errors=True)
        print(f"  ABBRUCH: unvollstaendig, {len(missing)} Dateien fehlen")
        return 2
    leaked = contamination(staged)
    if leaked:
        shutil.rmtree(staged, ignore_errors=True)
        print(f"  ABBRUCH: unerwuenschte Dateien: {leaked[:4]}")
        return 2
    staged_build, _ = compute_build(staged)
    if staged_build != build:
        shutil.rmtree(staged, ignore_errors=True)
        print(f"  ABBRUCH: Kopie ergibt {staged_build}, erwartet {build}")
        return 2
    print(f"  {len(MODULES)} Module bereitgestellt, Sperrliste geprueft, Hash bestaetigt")

    if not os.path.exists(os.path.join(PYTHON_DIR, "cpython-3.13-macos-aarch64-none")):
        run(["uv", "python", "install", "--install-dir", PYTHON_DIR, PYTHON_VERSION])
    if not os.path.exists(os.path.join(VENV, "bin", "python")):
        run(["uv", "venv", "--python", interpreter(), VENV])
    run(["uv", "pip", "install", "--python", os.path.join(VENV, "bin", "python"),
         *REQUIREMENTS])
    real = os.path.realpath(os.path.join(VENV, "bin", "python"))
    if real.startswith(os.path.expanduser("~/")):
        print(f"  ABBRUCH: der Interpreter liegt im Zuhause des Core: {real}")
        return 3

    switch(staged)
    prune(staged)
    for path in (TARGET, VENV, PYTHON_DIR, staged):
        os.chmod(path, 0o755)
    print(f"  umgeschaltet: app -> {os.path.basename(staged)}")

    check = subprocess.run(
        [os.path.join(VENV, "bin", "python"), "-c",
         "import sys; sys.path.insert(0, %r); "
         "from solvio.portal.build import installed_build; "
         "import solvio.portal.service as s; "
         "print(installed_build(s.LOADED_FROM))" % LINK],
        capture_output=True, text=True)
    reported = (check.stdout or "").strip()
    print(f"  Arbeiter meldet: {reported}")
    if reported != build:
        print(f"  ABBRUCH: gemeldeter Bauzustand weicht ab ({check.stderr.strip()[:120]})")
        return 4
    print("  Auslieferung vollstaendig und nachweisbar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
