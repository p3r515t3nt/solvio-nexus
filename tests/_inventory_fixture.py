"""Ein synthetischer Bestand — damit keine Suite den produktiven liest.

DEBT-0228, konkret. Die Offsite-Suiten bauen ihren Sicherungssatz aus
`storage.inventory.items()`. Diese Liste zeigt auf den laufenden Core:
`~/.solvio/conversations.sqlite3`, `~/.solvio/memory/*.sqlite3`, die
Freigabe-Identitaet unter `~/.solvio-approvals-production/` — und, unabhaengig
von `HOME`, auf `/Users/solvio/solvio-core/.env`.

Damit hatten diese Suiten nur zwei Ausgaenge: **die produktiven Datenbanken
lesen, oder durchfallen.** Ein angehaltener Dienst haette daran nichts
geaendert; eine reale Datenbank wird nicht dadurch zur Testdatei, dass gerade
niemand sie benutzt.

Dieses Modul legt stattdessen einen VOLLSTAENDIGEN Bestand an — dieselben
Namen, dieselbe Struktur, nur unter einer eigenen Wurzel und mit
ausschliesslich synthetischem Inhalt. Die Zusicherungen bleiben dieselben: es
wird nichts uebersprungen, nichts abgeschwaecht und keine Pflichtangabe
weggelassen. Was hier fehlt, faellt genauso auf wie vorher.
"""
from __future__ import annotations

import os
import sqlite3

#: Was in eine synthetische Datei kommt. Nie etwas, das wie ein Zugang aussieht.
_MARKER = "synthetic-inventory-fixture"


#: Die Tabellen, die der Wiederherstellungsbericht wirklich LIEST.
#:
#: Eine Fixture-Datenbank mit nur einer `fixture`-Tabelle waere keine: der
#: Bericht faende die Tabellen nicht, meldete `readable: False`, und die
#: Zusicherung ueber die anstehende Eigentuemer-Handlung liefe ins Leere. Der
#: synthetische Bestand traegt deshalb die Schemata, die gelesen werden — mit
#: synthetischen Zeilen, aber echter Form.
_SCHEMAS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "payments.sqlite3": (("instruments", "status", "active"),
                         ("intents", "state", "ready_for_approval")),
    "approval_control.sqlite3": (("approval_requests", "state", "CONSUMED"),),
    "agent_runs.sqlite3": (("agent_runs", "state", "SUCCEEDED"),),
}


def _sqlite(path: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS fixture (k TEXT, v TEXT)")
        conn.execute("INSERT INTO fixture (k, v) VALUES (?, ?)",
                     ("herkunft", _MARKER))
        for tabelle, spalte, wert in _SCHEMAS.get(os.path.basename(path), ()):
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {tabelle} "
                f"(id TEXT PRIMARY KEY, {spalte} TEXT NOT NULL)")
            conn.execute(f"INSERT OR REPLACE INTO {tabelle} (id, {spalte}) "
                         f"VALUES (?, ?)", (f"{_MARKER}-1", wert))
        conn.commit()
    finally:
        conn.close()
    os.chmod(path, 0o600)


def _file(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(path, 0o600)


def _tree(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    _file(os.path.join(path, f"{_MARKER}.txt"), _MARKER + "\n")


def build(root: str, *, optional: bool = True) -> str:
    """Den vollstaendigen Bestand unter `root` anlegen.

    Erzeugt wird nach der ECHTEN Liste, nicht nach einer abgeschriebenen: kommt
    ein Pflichtstueck dazu, entsteht es hier automatisch mit. Eine Suite, die
    ihre Fixture von Hand pflegt, faellt beim naechsten neuen Bestandsteil
    entweder falsch durch oder falsch durch — beides hilft niemandem.
    """
    from solvio.storage import inventory as INV

    vorher = os.environ.get(INV.ROOT_ENV)
    os.environ[INV.ROOT_ENV] = root
    try:
        for item in INV.items():
            if not item.required and not optional:
                continue
            ziel = item.expanded
            if item.kind == INV.SQLITE:
                _sqlite(ziel)
            elif item.kind == INV.TREE:
                _tree(ziel)
            else:
                _file(ziel, f"{_MARKER}: {item.name}\n")
    finally:
        if vorher is None:
            os.environ.pop(INV.ROOT_ENV, None)
        else:
            os.environ[INV.ROOT_ENV] = vorher
    return root


def activate(root: str, *, optional: bool = True) -> str:
    """Bestand anlegen UND die Wurzel setzen. Der uebliche Einstieg."""
    from solvio.storage import inventory as INV
    from solvio import git_binary
    from pathlib import Path
    import subprocess

    # Bundle the exact public producer, never a repository in the operator's
    # home. The iOS bundle is its own synthetic repository inside this fixture.
    INV.CORE_REPO = str(Path(__file__).resolve().parents[1])
    ios = Path(root).resolve() / "synthetic-ios-repository"
    ios.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    if not (ios / ".git").is_dir():
        (ios / "README.md").write_text(_MARKER + ": iOS repository\n", encoding="utf-8")
        for args in (("init", "--quiet"), ("add", "README.md"),
                     ("-c", "user.name=Synthetic Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "--quiet",
                      "-m", "Synthetic iOS backup fixture")):
            subprocess.run([git_binary.resolve(), "-C", str(ios), *args],
                           env=env, check=True, capture_output=True, timeout=30)
    INV.IOS_REPO = str(ios)

    build(root, optional=optional)
    os.environ[INV.ROOT_ENV] = root
    return root
