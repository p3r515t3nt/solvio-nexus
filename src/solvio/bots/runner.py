"""Wie ein Bot gestartet wird — im selben Gefaengnis, mit demselben Profil.

Es gibt hier bewusst **kein zweites Isolationsmodell**. Ein Bot laeuft unter
demselben Seatbelt-Profil wie der tiefe Executor, mit derselben aus dem Nichts
gebauten Umgebung, unter demselben `sandbox-exec`. Alles, was `deep/isolation.py`
zusichert — kein Repo, keine `.env` des Cores, kein `~/.ssh`, kein Zugang zu
8766/8770/8123 — gilt hier unveraendert, weil es dieselben Funktionen sind und
nicht dieselbe Idee.

Vier Dinge macht diese Datei zusaetzlich, und jedes hat einen Grund:

**Ein fester Aufruf.** Programmpfad und Argumentliste stehen vorher fest; es gibt
keine Zeichenkette, die eine Shell lesen koennte, und keine Stelle, an der ein
Modell einen Profilnamen unterschieben koennte.

**Die Frage geht ueber eine Datei.** `--query-file` ist Hermes' eigener Weg fuer
beliebigen Text: nichts daran wird interpretiert, und die Frage steht nicht in
der Prozessliste des Rechners. Die Datei liegt im Gefaengnis und wird danach
geloescht.

**Die Frist gehoert SOLVIO.** Hermes bekommt sein `--run-budget` etwas knapper,
damit es von selbst zusammenfasst; darueber liegt SOLVIOs eigene Frist, und die
raeumt die ganze Prozessgruppe ab. Ein Kind, das haengt, haengt nicht ewig.

**Die Ausgabe ist fremder Text.** Sie wird gedeckelt und entschaerft, bevor
irgendetwas damit passiert — auch bevor sie in ein Protokoll geraet.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from dataclasses import dataclass

from solvio.bots.redaction import redact
from solvio.deep import isolation
from solvio.logging_setup import get_logger

log = get_logger("bots")

#: Wie viel Text ein Bot zurueckgeben darf. Was darueber liegt, hat nicht mehr
#: gesagt — es hat nur mehr Kontext verbraucht.
MAX_OUTPUT = 200_000

#: Der Ordner im Gefaengnis, in dem Fragen kurz liegen.
WORK_SUBDIR = "bots"

#: Der Name des Seatbelt-Profils fuer Botlaeufe. Bewusst eine eigene Datei und
#: nicht die des Executors: derselbe Inhalt aus derselben Funktion, aber ohne
#: Schreibkollision mit einem laufenden Gateway.
PROFILE_NAME = "bots.sandbox.sb"


class JailUnavailable(RuntimeError):
    """Ohne Gefaengnis gibt es keine Bots. Kein Halbzustand, keine Attrappe."""


@dataclass(frozen=True)
class Jail:
    """Wo die Bots wohnen. Dieselben Pfade wie der tiefe Executor."""

    path: str
    venv_bin: str
    python_root: str

    @property
    def hermes_home(self) -> str:
        """Die Wurzel, unter der Hermes seine Profile fuehrt."""
        return os.path.join(self.path, "home")

    @property
    def profiles_root(self) -> str:
        return os.path.join(self.hermes_home, "profiles")

    @property
    def work(self) -> str:
        return os.path.join(self.path, "work", WORK_SUBDIR)

    @property
    def hermes(self) -> str:
        return os.path.join(self.venv_bin, "hermes")

    def profile_dir(self, profile: str) -> str:
        return os.path.join(self.profiles_root, profile)


def jail_from_environment() -> Jail | None:
    """Baut die Gefaengnisangaben aus derselben Umgebung wie der Executor.

    Ausdruecklich dieselben Variablen: es gibt ein Gefaengnis, nicht zwei. Wer
    hier eigene Namen einfuehrte, haette am Ende zwei Wahrheiten darueber, wo
    Hermes wohnt — und die zweite waere irgendwann die falsche.
    """
    path = (os.environ.get("SOLVIO_DEEP_JAIL", "") or "").strip()
    if not path or not os.path.isdir(path):
        log.info("bots.jail_missing", configured=bool(path))
        return None
    venv_bin = os.path.join(path, "venv", "bin")
    if not os.path.exists(os.path.join(venv_bin, "hermes")):
        log.info("bots.executor_not_installed")
        return None
    python_root = (os.environ.get("SOLVIO_DEEP_PYTHON_ROOT", "") or "").strip()
    if not python_root:
        python_root = isolation.interpreter_root(venv_bin)
    return Jail(path=path, venv_bin=venv_bin, python_root=python_root)


@dataclass
class Outcome:
    """Was ein Botlauf zurueckliess — Text, kein Vertrauen."""

    ok: bool
    text: str = ""
    reason: str = ""
    exit_code: int | None = None
    elapsed: float = 0.0
    truncated: bool = False


def _profile_path(jail: Jail) -> str:
    """Schreibt das Seatbelt-Profil und liefert seinen Pfad.

    Die Ports werden **hier** aufgeloest und nicht der Vorgabe ueberlassen: ein
    Botprofil, das eine andere Ausgangs- oder Bindezeile traegt als das Gateway,
    waere genau die Art von leiser Abweichung, die niemand bemerkt, bis eine
    Botfrage ohne Grund `403` bekommt.
    """
    os.makedirs(jail.work, mode=0o700, exist_ok=True)
    target = os.path.join(jail.work, PROFILE_NAME)
    body = isolation.render_profile(jail=jail.path, python_root=jail.python_root,
                                    broker_port=_broker_port(),
                                    gateway_port=_gateway_port())
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(body)
    return target


def _broker_port() -> int:
    from solvio.provider_broker.service import configured_port

    return configured_port()


def _gateway_port() -> int:
    raw = (os.environ.get("SOLVIO_DEEP_PORT", "") or "").strip()
    return int(raw) if raw.isdigit() else isolation.GATEWAY_PORT


async def run(jail: Jail, argv: list[str], *, timeout: float) -> Outcome:
    """Fuehrt genau einen Hermes-Aufruf im Gefaengnis aus.

    `argv` sind die Argumente NACH `hermes`. Eine Liste, nie eine Zeichenkette —
    es gibt damit keine Shell, die etwas interpretieren koennte.
    """
    if not os.path.exists(isolation.SANDBOX_EXEC):
        raise JailUnavailable("sandbox-exec missing")
    if not os.path.exists(jail.hermes):
        raise JailUnavailable("executor binary missing")

    profile_path = _profile_path(jail)
    env = isolation.child_environment(jail=jail.path, hermes_home=jail.hermes_home)
    leaking = isolation.leaking_names(env)
    if leaking:
        # Kein `assert`: unter `python -O` waere die Pruefung weg — und ein
        # Geheimnis genau dort, wo es am meisten schadet.
        raise JailUnavailable(f"child environment carries {len(leaking)} forbidden names")

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            isolation.SANDBOX_EXEC, "-f", profile_path, jail.hermes, *argv,
            cwd=jail.path, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
    except (OSError, ValueError) as exc:
        log.warning("bots.spawn_failed", kind=type(exc).__name__)
        return Outcome(False, reason="spawn_failed")

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        # Die ganze Prozessgruppe: Hermes startet Helfer, und ein verwaister
        # Helfer haelt Kontingent und Speicher fest.
        _terminate(process)
        return Outcome(False, reason="timeout", elapsed=time.monotonic() - started)
    except asyncio.CancelledError:
        _terminate(process)
        raise
    except Exception as exc:  # noqa: BLE001
        _terminate(process)
        log.warning("bots.communicate_failed", kind=type(exc).__name__)
        return Outcome(False, reason="communication_failed")

    raw = (stdout or b"").decode("utf-8", "replace")
    truncated = len(raw) > MAX_OUTPUT
    if truncated:
        # Die Antwort steht am ENDE. Wer vorne abschneidet, wirft sie weg.
        raw = raw[-MAX_OUTPUT:]
    text = redact(raw)
    return Outcome(
        ok=process.returncode == 0,
        text=text,
        reason="" if process.returncode == 0 else "nonzero_exit",
        exit_code=process.returncode,
        elapsed=time.monotonic() - started,
        truncated=truncated)


def _terminate(process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


class QuestionFile:
    """Eine Frage, die genau so lange existiert wie der Aufruf."""

    def __init__(self, jail: Jail, text: str) -> None:
        os.makedirs(jail.work, mode=0o700, exist_ok=True)
        self.path = os.path.join(jail.work, f"frage-{uuid.uuid4().hex}.txt")
        previous = os.umask(0o077)
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(text)
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)

    def cleanup(self) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def __enter__(self) -> "QuestionFile":
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()
