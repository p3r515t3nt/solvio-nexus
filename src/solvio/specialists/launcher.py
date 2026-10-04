"""Wie ein Spezialist gestartet wird — und was er dabei ausdruecklich NICHT bekommt.

Zwei gemessene Tatsachen haben diese Datei erzwungen.

**Erstens:** Hermes kann die Spezialisten gar nicht starten. Sein Seatbelt-Profil
erlaubt `process-exec` nur unterhalb des uv-Python und des Gefaengnisses, sein
`PATH` ist `/usr/bin:/bin`, und `~/.claude` wie `~/.codex` liegen ausserhalb jedes
erlaubten `file-read*`-Pfades. Claude Code oder Codex „durch Hermes" laufen zu
lassen hiesse, genau diese Isolation aufzubrechen. Also startet sie der Core —
und das Modell im Gefaengnis bekommt davon nichts, nicht einmal einen Pfad.

**Zweitens:** beide Werkzeuge koennen Abo- oder API-Anmeldungen verwenden. Die
Sperrliste entfernt geerbte API-Zugaenge, der vorgelagerte Anmeldestatus prueft
zusaetzlich gespeicherte Anmeldungen. Beides zusammen beweist noch keine
Kostenfreiheit: Abo-Zusatzverbrauch/Usage Credits haben eigene Bedingungen.
Fuer Agentenauftraege verlangt cost_dispatch deshalb vor jedem Aufruf einen
Kostenbeleg. Fast Mode wird in der Kindumgebung unabhaengig vom Elternwert
deaktiviert; das allein ersetzt den Zusatzkostenbeleg ebenfalls nicht.

Der Rest folgt daraus: feste Programmpfade statt Suche im `PATH`, eine Argumentliste
statt einer Kommandozeile, die Frage ueber `stdin` statt ueber `argv`, feste
Fristen, feste Ausgabegrenzen. Es gibt keinen Weg, hier ein beliebiges Kommando
unterzubringen — nicht weil es verboten waere, sondern weil keine Stelle existiert,
an der ein Kommando entstehen koennte.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field

from solvio.logging_setup import get_logger

log = get_logger("specialists")

#: Was ein Spezialist an Umgebung bekommt. Fuenf Namen, wie beim Gefaengnis auch.
#: `HOME` muss dabei sein: dort liegt die Sitzung des Abonnements, die das
#: Werkzeug selbst liest. SOLVIO liest sie nie.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "SHELL",
                 "USER", "LOGNAME")

#: Die Sperrliste, die aus einer Zusage einen Mechanismus macht.
#:
#: Steht einer dieser Namen in der Kindumgebung, kann das Werkzeug auf
#: Abrechnung nach Verbrauch ausweichen — lautlos, und genau dann, wenn das
#: Kontingent des Abonnements zu Ende ist. Also steht keiner drin. Der Core
#: selbst braucht `OPENAI_API_KEY` fuer den Sprachweg; hier wird er entfernt.
DENIED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION",
    "CODEX_API_KEY", "AZURE_OPENAI_API_KEY",
    # Nichts aus SOLVIO selbst hat in einem Spezialisten etwas zu suchen.
    "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL",
    "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_CLIENT_ID", "SOLVIO_DEEP_JAIL", "SOLVIO_DEEP_STATE_DIR",
)

#: Wie viel Text ein Spezialist zurueckgeben darf. Ein Modell, das zehn Megabyte
#: liefert, hat nicht mehr gesagt — es hat nur mehr Kontext verbraucht.
MAX_OUTPUT = 60_000

#: Muster, die nach Geheimnis aussehen. Die Ausgabe eines fremden Werkzeugs ist
#: fremder Text; er koennte einen Schluessel enthalten, weil das Werkzeug ihn in
#: einer Fehlermeldung nennt. Was hier durchrutscht, steht danach im Journal.
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(?:oauth|bearer|access|refresh)[_-]?token\b\s*[:=]\s*\S+",
               re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
)

MASK = "<entfernt>"


class LauncherError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Invocation:
    """Ein fester Aufruf. Alles daran ist vorher bekannt."""

    #: Der absolute Pfad des Programms. Kein Name, der im `PATH` gesucht wird —
    #: sonst entscheidet die Umgebung, welches Programm laeuft.
    executable: str
    #: Die Argumente NACH dem Programmnamen. Eine Liste, nie eine Zeichenkette:
    #: es gibt damit keine Shell, die etwas interpretieren koennte.
    argv: tuple[str, ...]
    timeout: float
    #: Die Frage geht ueber `stdin`. In `argv` waere sie in jeder Prozessliste
    #: des Rechners sichtbar.
    prompt_via_stdin: bool = True
    cwd: str = "/"
    #: Nur vom Core gesetzte native Codex-Ablage. Kein Token und keine freie
    #: Kindumgebung; Statuspruefung und Ausfuehrung verwenden dieselbe Ablage.
    codex_home: str = ""
    #: Ein nativer Worker kann vor dem Gruppen-Kill turn/interrupt senden.
    shutdown_grace: float = 0.0
    cleanup_group: bool = False


@dataclass
class Outcome:
    """Was zurueckkam — Text, kein Vertrauen."""

    ok: bool
    text: str = ""
    reason: str = ""
    exit_code: int | None = None
    elapsed: float = 0.0
    truncated: bool = False
    stderr_note: str = ""
    #: Gemessen am Starter, nicht aus einem Fehlerwort geraten. None bleibt
    #: fuer alte Adapter ungewiss; nur False beweist einen Nichtstart.
    process_started: bool | None = None


def redact(text: str) -> str:
    """Entfernt, was nach Anmeldedaten aussieht — bevor es irgendwo landet."""
    cleaned = text or ""
    for shape in _SECRET_SHAPES:
        cleaned = shape.sub(MASK, cleaned)
    return cleaned


def child_environment() -> dict[str, str]:
    """Die Umgebung eines Spezialisten: Erlaubnisliste minus Sperrliste.

    Die Reihenfolge ist wichtig und absichtlich doppelt gesichert: erst wird nur
    uebernommen, was auf der Erlaubnisliste steht, danach wird trotzdem noch
    einmal gegen die Sperrliste geprueft. Die zweite Pruefung ist theoretisch
    ueberfluessig — bis jemand der Erlaubnisliste einen Namen hinzufuegt.
    """
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    for name in DENIED_ENV:
        env.pop(name, None)
    env["PATH"] = _child_path(env.get("PATH", ""))
    # Fast Mode kann neben einem Max-Abo Usage Credits verbrauchen. Ein Wert
    # aus der Elternumgebung darf diese feste Kindgrenze niemals lockern.
    env["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
    # The native billing reader pins the reviewed CLI. A physical invocation
    # must not auto-update that executable after its final context check.
    env["DISABLE_AUTOUPDATER"] = "1"
    leaking = [name for name in env if name in DENIED_ENV]
    if leaking:
        raise LauncherError("environment_leak", f"{len(leaking)} names")
    return env


def brokered_environment(*, base_url: str, token: str,
                         tmpdir: str = "", config_dir: str = "") -> dict[str, str]:
    """Die Umgebung des **gemakelten** Claude-Builders (V0.6).

    Sie ist `child_environment()` plus genau zwei Namen — und beide stehen auf
    der Sperrliste. Das ist kein Widerspruch, sondern der Punkt: die Sperrliste
    verhindert, dass die Umgebung des Cores durchsickert. Was hier gesetzt
    wird, kommt NICHT aus `os.environ`, sondern aus dem Aufruf:

    * `ANTHROPIC_BASE_URL` — die Rueckschleife zum eigenen Broker. Der Kaefig
      laesst ohnehin kein anderes Ziel zu; diese Zeile sagt der CLI nur, wohin.
    * `ANTHROPIC_API_KEY` — das **Broker-Token**, kurzlebig und leasegebunden.
      Ohne offenes Lease oeffnet es nichts, und nach dem Lease-Schluss ist es
      `401`.

    Fail-closed an beiden Werten: eine Basis, die nicht auf die Rueckschleife
    zeigt, und ein leeres Token sind Fehler, keine Vorgaben. Sonst waere die
    stille Fehlbedienung genau die, bei der das CLI doch zum echten Anbieter
    spricht.

    `config_dir` (N8/C4, Claude-Auftragsarbeiter) verlegt den GESAMTEN
    CLI-Zustand (`CLAUDE_CONFIG_DIR`: Sitzungen, Projekte, `.claude.json`) in
    den Kaefig — gemessen am 2026-09-18: `~/.claude/projects` bekommt keinen
    Eintrag, die Sitzungsdatei liegt unter `<config_dir>/projects/`. Dazu
    `DISABLE_TELEMETRY`/`DISABLE_ERROR_REPORTING` wie beim Nutzungsleser.
    Der V0.6-Builder ohne `config_dir` bleibt Wort fuer Wort gleich.
    """
    if not token:
        raise LauncherError("brokered_env_incomplete", "kein Broker-Token")
    if not (base_url.startswith("http://127.0.0.1:")
            or base_url.startswith("http://localhost:")):
        raise LauncherError("brokered_env_not_loopback", base_url[:40])
    env = child_environment()
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_API_KEY"] = token
    if config_dir:
        if not os.path.isabs(config_dir):
            raise LauncherError("brokered_env_config_dir_relative")
        env["CLAUDE_CONFIG_DIR"] = config_dir
        env["DISABLE_TELEMETRY"] = "1"
        env["DISABLE_ERROR_REPORTING"] = "1"
    if tmpdir:
        # Das Laufzeitverzeichnis der CLI **in den Kaefig** holen.
        #
        # Live gemessen am 2026-09-02: ohne das scheitert der Start mit
        # `EEXIST: mkdir '/tmp/claude-501'`. Die CLI legt dort ein
        # Verzeichnis JE BENUTZERKENNUNG an — geteilt mit den interaktiven
        # Sitzungen des Eigentuemers. Es dem Kaefig zu oeffnen waere die
        # bequeme Loesung und die falsche: ein modellgesteuerter Schreiber
        # koennte dort Dateien hinterlegen, die ein anderer Claude-Prozess
        # liest.
        #
        # `CLAUDE_CODE_TMPDIR` verlegt es. Das ist eine VERENGUNG, keine
        # Ausnahme: der Builder bekommt sein eigenes Laufzeitverzeichnis
        # innerhalb der Grenzen, die er ohnehin hat.
        env["TMPDIR"] = tmpdir
        env["CLAUDE_CODE_TMPDIR"] = tmpdir
    return env


def _child_path(inherited: str) -> str:
    """Der Suchpfad des Kindes — um `STANDARD_BINARIES` ergaenzt.

    `resolve()` findet ein Werkzeug auch dann, wenn `PATH` es nicht kennt. Das
    genuegt fuer das Werkzeug selbst, aber nicht fuer das, was es STARTET.

    Live gemessen: `codex` ist ein Node-Skript mit `#!/usr/bin/env node`. Unter
    launchd ist `PATH` `/usr/bin:/bin:/usr/sbin:/sbin`, Homebrew fehlt darin,
    und jeder Bauauftrag scheiterte mit `env: node: No such file or directory` —
    dreimal hintereinander, bis die Grenze erreicht war. Aus einer Shell
    gestartet lief derselbe Code, weil dort ein anderer `PATH` vererbt wird.

    Dasselbe Muster wie damals beim Fachteam, eine Ebene tiefer: dort war das
    Werkzeug nicht auffindbar, hier ist es der Interpreter, den das Werkzeug
    braucht.

    Die Ergaenzung ist ANGEHAENGT, nicht vorangestellt: ein vererbter `PATH`
    behaelt seinen Vorrang, und die Liste steht als Code, nicht als Umgebung —
    ein Suchpfad, den ein Aufrufer setzen kann, waere ein Weg, ein
    untergeschobenes Werkzeug ausfuehren zu lassen.
    """
    teile = [p for p in (inherited or "").split(os.pathsep) if p]
    for folder in STANDARD_BINARIES:
        if folder not in teile and os.path.isdir(folder):
            teile.append(folder)

    # Die eine Ausnahme von der Anhaenge-Regel: die gepruefte git-Binary muss
    # VORNE stehen (P0.2).
    #
    # `/usr/bin/git` ist auf macOS kein git, sondern der xcselect-Weiterleiter.
    # Im Kaefig darf er das Entwicklerverzeichnis nicht lesen, haelt die
    # Werkzeuge fuer nicht installiert und oeffnet den GUI-Installer — gemessen
    # am 2026-09-02, neun Kernel-Verweigerungen und fuenf nutzlose
    # Installationen. Stuende das Verzeichnis hinten, gaebe `/usr/bin` weiterhin
    # den Weiterleiter zurueck.
    #
    # Das widerspricht der Regel oben nicht: dieser Pfad kommt nicht aus der
    # Umgebung, sondern aus `git_binary.resolve()` — und der hat ihn vorher
    # geprueft (Eigentuemer, Rechte, Signatur, Probelauf).
    try:
        from solvio import git_binary as _GB
        vertraut = os.path.dirname(_GB.resolve())
    except Exception:                                    # noqa: BLE001
        vertraut = ""
    if vertraut and os.path.isdir(vertraut):
        teile = [vertraut] + [p for p in teile if p != vertraut]

    return os.pathsep.join(teile) or os.pathsep.join(STANDARD_BINARIES)


#: Wo Werkzeuge liegen, wenn PATH sie nicht kennt.
#:
#: Unter launchd ist PATH `/usr/bin:/bin:/usr/sbin:/sbin` — Homebrew fehlt
#: darin. Das Fachteam war im Dienstbetrieb damit vollstaendig tot und lief nur,
#: wenn SOLVIO aus einer Shell gestartet wurde. Aufgefallen ist das dem Arzt.
#:
#: Die Liste steht als Code hier und kommt ausdruecklich NICHT aus der Umgebung.
#: Ein Suchpfad, den ein Aufrufer setzen kann, waere ein Weg, SOLVIO ein
#: untergeschobenes `claude` ausfuehren zu lassen — dieselbe Ueberlegung wie bei
#: den Vorgehen des Arztes: Code, nicht Text.
STANDARD_BINARIES = ("/opt/homebrew/bin", "/usr/local/bin",
                     os.path.expanduser("~/.local/bin"))


def resolve(program: str) -> str:
    """Der absolute Pfad eines Werkzeugs, einmal aufgeloest und geprueft."""
    found = shutil.which(program)
    if not found:
        for folder in STANDARD_BINARIES:
            candidate = os.path.join(folder, program)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found:
        raise LauncherError("not_installed", program)
    if not os.path.isabs(found):
        raise LauncherError("not_absolute", program)
    if not os.access(found, os.X_OK):
        raise LauncherError("not_executable", program)
    return found


async def _stream_communicate(process, payload, on_line, retain=True):
    """One bounded reader per pipe; callbacks complete before process return.

    Used by the native JSONL worker (retain=True) and the Claude stream
    consumer (retain=False). A split UTF-8 sequence is decoded only after its
    complete line; stderr never becomes a progress event.

    retain=True: overflow of the WHOLE stdout beyond the ceiling is drained
    without retaining it and invalidates the outcome (today's behaviour).
    retain=False (N8/C4): nothing is retained; every complete line is
    delivered as it arrives. Only a SINGLE line above the ceiling is discarded
    and marks the outcome truncated — the lines after it keep arriving, so the
    terminal `result` event of a long tool-heavy turn is still seen.
    """
    ceiling = MAX_OUTPUT * 4
    overflow = False

    async def output():
        nonlocal overflow
        retained, pending = bytearray(), bytearray()
        accepting = True
        discarding = False
        while chunk := await process.stdout.read(8192):
            if not accepting:
                continue
            if retain:
                if len(retained) + len(chunk) > ceiling:
                    overflow = True
                    accepting = False
                    pending.clear()
                    continue
                retained.extend(chunk)
            pending.extend(chunk)
            while (end := pending.find(b'\n')) >= 0:
                line = bytes(pending[:end])
                del pending[:end + 1]
                if discarding:
                    # Der Rest einer verworfenen Ueberlaenge: nicht zustellen.
                    discarding = False
                    continue
                if not retain and len(line) > ceiling:
                    # Die Ueberlaenge kam samt Zeilenende in einem Stueck.
                    overflow = True
                    continue
                on_line(line.decode('utf-8', 'strict'))
            if not retain and len(pending) > ceiling:
                # Eine Einzelzeile ueber der Grenze wird verworfen, nicht
                # gesammelt: der Speicher bleibt begrenzt, der Strom laeuft.
                overflow = True
                discarding = True
                pending.clear()
        if pending and accepting and not discarding:
            on_line(pending.decode('utf-8', 'strict'))
        return bytes(retained)

    async def errors():
        nonlocal overflow
        retained = bytearray()
        while chunk := await process.stderr.read(8192):
            room = max(0, ceiling - len(retained))
            retained.extend(chunk[:room])
            overflow |= len(chunk) > room
        return bytes(retained)

    async def input_and_wait():
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
        await process.wait()

    jobs = [asyncio.create_task(output()), asyncio.create_task(errors()),
            asyncio.create_task(input_and_wait())]
    try:
        stdout, stderr, _ = await asyncio.gather(*jobs)
        return stdout, stderr, overflow
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


def _brokered_only(environment: dict[str, str]) -> dict[str, str]:
    """Nur eine `brokered_environment()` darf hier hinein — geprueft, nicht
    angenommen: kein Sperrlisten-Name ausser den zwei Broker-Werten, die Basis
    auf der Rueckschleife, Fast Mode aus. Alles andere ist ein Leck."""
    if type(environment) is not dict:
        raise LauncherError("environment_leak", "kein dict")
    leaking = [name for name in environment if name in DENIED_ENV
               and name not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")]
    if leaking:
        raise LauncherError("environment_leak", f"{len(leaking)} names")
    base = environment.get("ANTHROPIC_BASE_URL", "")
    if not (base.startswith("http://127.0.0.1:") or base.startswith("http://localhost:")):
        raise LauncherError("brokered_env_not_loopback", base[:40])
    if not environment.get("ANTHROPIC_API_KEY") or environment.get("CLAUDE_CODE_DISABLE_FAST_MODE") != "1":
        raise LauncherError("brokered_env_incomplete")
    return dict(environment)


async def run(invocation: Invocation, prompt: str, *, on_stdout_line=None,
              retain_stdout: bool = True, environment: dict[str, str] | None = None) -> Outcome:
    """Fuehrt einen festen Aufruf aus. Kein Shell, kein Kommando aus Text.

    `retain_stdout=False` (N8/C4, nur mit `on_stdout_line`): die Zeilen werden
    zugestellt und NICHT behalten; `Outcome.text` bleibt leer, der Konsument
    haelt das Ergebnis. Der Codex-Pfad (Vorgabe `True`) bleibt unveraendert.

    `environment` (N8/C4): die Kindumgebung des GEMAKELTEN Claude-Arbeiters —
    ausschliesslich das Ergebnis von `brokered_environment()` (Broker-Token
    statt Anbieterschluessel, CLI-Zustand im Kaefig). Sie wird geprueft, nie
    uebernommen; ohne sie gilt `child_environment()` wie bisher.
    """
    import time
    started = time.monotonic()
    try:
        env = child_environment() if environment is None else _brokered_only(environment)
        if invocation.codex_home:
            if not os.path.isabs(invocation.codex_home):
                raise LauncherError("invalid_native_home")
            env["CODEX_HOME"] = invocation.codex_home
    except LauncherError as exc:
        return Outcome(False, reason=exc.reason, process_started=False)

    try:
        process = await asyncio.create_subprocess_exec(
            invocation.executable, *invocation.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=invocation.cwd, start_new_session=True)
    except (OSError, ValueError) as exc:
        log.warning("specialist.spawn_failed", kind=type(exc).__name__)
        return Outcome(False, reason="spawn_failed", process_started=False)

    payload = (prompt or "").encode("utf-8") if invocation.prompt_via_stdin else b""
    try:
        overflow = False
        if on_stdout_line is None:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=invocation.timeout)
        else:
            stdout, stderr, overflow = await asyncio.wait_for(
                _stream_communicate(process, payload, on_stdout_line,
                                    retain=retain_stdout),
                timeout=invocation.timeout)
    except asyncio.TimeoutError:
        # Die ganze Prozessgruppe, nicht nur das Kind: ein CLI startet gern
        # Helfer, und ein verwaister Helfer haelt Kontingent und Speicher fest.
        await _stop(process, invocation.shutdown_grace)
        return Outcome(False, reason="timeout",
                       elapsed=time.monotonic() - started, process_started=True)
    except asyncio.CancelledError:
        # Ein uebergeordneter Auftrag kann auch VOR der CLI-Frist abbrechen.
        # Cancellation ist BaseException: der Fehlerzweig unten faengt sie
        # nicht. Erst die gesamte Gruppe beenden, dann Abbruch weiterreichen.
        await _stop(process, invocation.shutdown_grace)
        raise
    except Exception as exc:  # noqa: BLE001
        await _stop(process, invocation.shutdown_grace)
        log.warning("specialist.communicate_failed", kind=type(exc).__name__)
        return Outcome(False, reason="communication_failed", process_started=True)

    if invocation.cleanup_group:
        # Der Gruppenleiter kann bereits beendet sein. Seine PID war durch
        # start_new_session zugleich die PGID; getpgid(dead_pid) waere zu spaet.
        _terminate(process)
    text = redact(stdout.decode("utf-8", "replace"))
    truncated = overflow or len(text) > MAX_OUTPUT
    note = redact(stderr.decode("utf-8", "replace")).strip()
    return Outcome(
        ok=process.returncode == 0,
        text=text[:MAX_OUTPUT],
        reason="" if process.returncode == 0 else "nonzero_exit",
        exit_code=process.returncode,
        elapsed=time.monotonic() - started,
        process_started=True,
        truncated=truncated,
        # Nur der Anfang, und schon entschaerft. `stderr` ist die Stelle, an der
        # ein CLI seine Konfiguration ausplaudert.
        stderr_note=note[:400])


async def _stop(process, grace: float = 0.0) -> None:
    # The owner can cancel while Core shutdown cancels the enclosing tick.
    # A second cancellation must not cut the process-group cleanup in half.
    cleanup = asyncio.create_task(_stop_process(process, grace))
    interrupted = False
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


async def _stop_process(process, grace: float = 0.0) -> None:
    import contextlib
    import signal
    if grace > 0 and process.returncode is None:
        # Nur der Worker erhaelt TERM: er unterbricht zuerst seinen nativen
        # Turn. TERM an alle Kinder zugleich wuerde diesen Weg abschneiden.
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=min(grace, 5.0))
    _terminate(process)
    await process.wait()


def _terminate(process) -> None:
    import contextlib
    import signal
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
