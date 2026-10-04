"""Wo der fremde Executor laufen darf — und was er dort nicht findet.

Der Auftrag ist eindeutig: Hermes laeuft nicht im Core-Prozess. Auf diesem Mac
gibt es keine Container-Laufzeit; was es gibt, ist Seatbelt (`sandbox-exec`),
und das ist kein Trostpreis: die Verweigerungen kommen als `EPERM` aus dem Kern,
sie stehen im Systemprotokoll, und ein Profil laesst sich von innen nicht
aufweiten — der Versuch endet mit `sandbox_apply: Operation not permitted`.
Profile schneiden sich, sie addieren sich nicht. Eine Einbahnstrasse.

Drei Grenzen, die getrennt gezogen werden muessen, weil keine die andere ersetzt:

**Dateien.** Standard ist Verweigerung. Lesbar ist das System, die
Python-Installation und das Gefaengnis; schreibbar ist ausschliesslich das
Gefaengnis. `.env`, `~/.solvio-approvals/`, `~/.solvio/` und das Repo sind auch
fuer `stat` gesperrt — `file-read-metadata` ist hier bewusst auf eine Liste
begrenzt und nicht global erlaubt, sonst laesst sich aus Groesse und Rechten
einer unlesbaren Datei noch immer etwas ablesen.

**Netz.** Nicht alles-oder-nichts. Erlaubt sind 443, 80, DNS — und **genau ein**
Rueckschleifen-Ziel: der Provider Broker des Cores auf `localhost:{broker_port}`.
Damit sind der Core (8766), der Freigabeweg (8770) und Home Assistant (8123)
aus dem Gefaengnis heraus weiterhin nicht erreichbar, und zwar nicht, weil sie
Anmeldedaten verlangen, sondern weil das Paket nie hinausgeht. Niemals
`localhost:*` — das oeffnete alle drei auf einmal. DNS laeuft auf macOS ueber
den Unix-Socket von `mDNSResponder`, der ebenfalls unter `network-outbound`
faellt — ohne diese eine Zeile scheitert jede Aufloesung mit einem `gaierror`,
das nach einem Netzproblem aussieht und keines ist.

Zur Ehrlichkeit gehoert: das `*` in `(remote tcp "*:443")` ist das **Wirtsfeld**,
nicht der Port. Der Kaefig erreicht also `127.0.0.1:443` und `:80` schon heute.
Die Rueckschleife ist nicht grundsaetzlich zu — sie war zu **fuer 8792**. Genau
deshalb bekommt der Broker einen eigenen, pruefbaren Port und sitzt nicht auf
443.

**Und die Gegenrichtung.** Bis zu diesem Milestone lizenzierte das Profil dem
Kaefig `network-bind` und `network-inbound` auf `localhost:*` — jeder Prozess im
Kaefig durfte **jeden** Rueckschleifen-Port selbst binden (DEBT-0127). Fuer den
Kaefig ist der Broker an nichts als der Portnummer erkennbar: blankes HTTP,
keine Server-Authentisierung. Wer 8792 zuerst bindet, **ist** der Broker, soweit
der Kaefig das beurteilen kann. Die Ausgangszeile allein haette diese Luecke
darum von einer theoretischen zu einer begehbaren gemacht — ein Kaefigprozess
bindet, ein zweiter verbindet sich. Beide Aenderungen gehoeren deshalb in
**dieselbe** Aenderung, und Binden und Eingang sind jetzt auf den einen
legitimen Lauscher des Kaefigs festgezurrt: das Gateway auf
`localhost:{gateway_port}`.

**Umgebung.** Seatbelt fasst Umgebungsvariablen nicht an; das muss der Elternteil
tun. Deshalb wird die Kindumgebung aus dem Nichts gebaut und nicht gefiltert.
Eine Sperrliste waere hier der falsche Reflex — der eingefrorene Freigabepfad
hat dieselbe Lehre schon einmal bezahlt und notiert sie als „eine Erlaubnisliste,
weil die vorherige Sperrliste unvollstaendig war".

Ehrlich zur Grenze: das Kind laeuft unter derselben Kennung wie der Core. Gegen
Dateien, Netz und `exec` schuetzt der Kern; ein echter Ausbruch aus Seatbelt
waere ein vollstaendiger Ausbruch. Eine virtuelle Maschine waere staerker.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("deep")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Der Rueckschleifen-Port des Provider Brokers — das EINZIGE Rueckschleifen-Ziel,
#: das der Kaefig erreichen darf.
BROKER_PORT = 8792

#: Der einzige Rueckschleifen-Lauscher, den der Kaefig selbst oeffnen darf: sein
#: eigenes Gateway.
GATEWAY_PORT = 8791

#: Die einzigen Variablen, die das Kind bekommt. Alles andere existiert dort
#: nicht — auch nicht leer.
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "HERMES_HOME")

#: Namen, deren Auftauchen in einer Kindumgebung ein Fehler waere. Die Liste ist
#: kein Filter (gefiltert wird nicht, es wird neu gebaut) — sie ist die Zusage,
#: gegen die Tests und der Start selbst pruefen duerfen.
FORBIDDEN_ENV = (
    "OPENAI_API_KEY", "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL",
    "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN", "GOOGLE_CALENDAR_ID",
    "SOLVIO_APPROVAL_STATE_DIR", "SOLVIO_APPROVAL_HOST", "SOLVIO_APPROVAL_PORT",
    "SOLVIO_RUNTIME_MODE", "SOLVIO_APPROVAL_V1", "SOLVIO_STATE_DIR",
    "SOLVIO_SATELLITE_AUTH_FILE", "SOLVIO_NODES_CONFIG", "SOLVIO_APP_ATTEST_TEAM",
    "SOLVIO_APP_ATTEST_BUNDLE", "SOLVIO_APP_ATTEST_DECISION_V1",
    "SOLVIO_APP_ATTEST_ENROLLMENT_V1", "SOLVIO_EMBEDDING", "SOLVIO_SATELLITE_BIND",
    "SOLVIO_VAULT_DIR", "SOLVIO_VAULT_TEST_KEYSTORE",
    # Derselbe Grund wie beim Tresorschalter nebenan: waer er im Kaefig
    # gesetzt, laege der Portalschluessel als DATEI in einem Verzeichnis, das
    # der Kaefigprozess selbst waehlt — und `~/.solvio-portal` steht drei
    # Zeilen weiter unten nicht ohne Grund unter SEALED_PATHS.
    "SOLVIO_PORTAL_TEST_KEYSTORE",
    # Der Kaefig soll den Broker nicht aus seiner Umgebung lernen. Er bekommt
    # die Adresse ueber `OPENAI_BASE_URL` in seiner `.env` und sonst nirgends.
    "SOLVIO_BROKER_PORT",
)

#: Pfade, die aus dem Gefaengnis heraus unerreichbar bleiben muessen. Sie stehen
#: hier, damit ein Test sie einzeln nachpruefen kann, statt dem Profil zu glauben.
SEALED_PATHS = (
    "~/solvio-core/.env",
    "~/.solvio-portal",
    "~/.solvio-vault",
    "~/.solvio-approvals",
    # Das produktive Freigabeverzeichnis heisst anders als der Modulstandard
    # (DEBT-0111/DEBT-0112). Beide stehen hier: eine Zusicherung, die nur den
    # Pfad prueft, den niemand benutzt, sichert nichts.
    "~/.solvio-approvals-production",
    "~/.solvio",
    "~/.ssh",
    "~/solvio-core",
)

_PROFILE = """(version 1)
(deny default)

;; --- Prozess -----------------------------------------------------------
(allow process-fork)
(allow process-exec (subpath "{python_root}") (subpath "{jail}"))
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))

;; --- Pfadaufloesung; bewusst als Liste, nicht global (sonst Metadatenleck) ---
(allow file-read-metadata
  (literal "/") (literal "/etc") (literal "/var") (literal "/tmp")
  (subpath "/usr") (subpath "/System") (subpath "/bin") (subpath "/dev")
  (subpath "/private/etc") (subpath "/private/var/db") (subpath "/private/var/run")
  (subpath "{python_root}")
  (subpath "{jail}"))

;; --- Lesen -------------------------------------------------------------
(allow file-read*
  (literal "/")                          ;; dyld liest das Wurzelverzeichnis selbst
  (subpath "/usr") (subpath "/System") (subpath "/bin")
  (subpath "/private/var/db")
  (subpath "/private/etc/ssl")
  (literal "/private/etc/hosts") (literal "/private/etc/resolv.conf")
  (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/dtracehelper")
  (subpath "{python_root}")
  (subpath "{jail}"))

;; --- Schreiben: genau ein Verzeichnis ----------------------------------
(allow file-write* (subpath "{jail}"))
(allow file-write-data
  (literal "/dev/null") (literal "/dev/dtracehelper")
  (literal "/dev/stdout") (literal "/dev/stderr"))

;; --- Netz: HTTPS, HTTP, DNS, und der Broker. Nichts sonst. -------------
;; Binden und Eingang NUR auf dem eigenen Gateway-Port. Ein Platzhalter-Port
;; hier hiesse: jeder Kaefigprozess darf den Broker-Port vorbelegen und sich
;; als Broker ausgeben. Siehe den Modul-Docstring.
(allow network-bind (local ip "localhost:{gateway_port}"))
(allow network-inbound (local ip "localhost:{gateway_port}"))
(allow network-outbound
  (remote tcp "*:443")
  (remote tcp "*:80")
  (remote tcp "*:53")
  (remote udp "*:53")
  (remote tcp "localhost:{broker_port}")     ;; der Provider Broker des Cores
  (path "/private/var/run/mDNSResponder"))   ;; DNS laeuft ueber diesen Socket
(allow system-socket)
"""


def interpreter_root(venv_bin: str) -> str:
    """Das Verzeichnis, das der Interpreter des Gefaengnisses wirklich braucht.

    Klingt nach Kleinkram, ist aber genau die Stelle, an der ein Profil still
    scheitert: `venv/bin/python` zeigt bei uv auf einen Alias ohne Patchnummer
    (`cpython-3.13-…`), waehrend `realpath` beim konkreten Bau landet
    (`cpython-3.13.15-…`). Erlaubt man nur den aufgeloesten Pfad, verweigert der
    Kern schon das `execvp` — mit einer Meldung, die nach einem Rechteproblem
    aussieht und ein Pfadproblem ist. Liegt der Interpreter im uv-Speicher, wird
    deshalb dieser Speicher freigegeben: er enthaelt Interpreter und sonst
    nichts, und beide Schreibweisen liegen darin.
    """
    resolved = os.path.realpath(os.path.join(venv_bin, "python3"))
    parts = resolved.split(os.sep)
    if "uv" in parts:
        index = parts.index("uv")
        if len(parts) > index + 1 and parts[index + 1] == "python":
            return os.sep.join(parts[:index + 2])
    return os.path.dirname(os.path.dirname(resolved))


def render_profile(*, jail: str, python_root: str,
                   broker_port: int = BROKER_PORT,
                   gateway_port: int = GATEWAY_PORT) -> str:
    """Baut das Seatbelt-Profil fuer genau dieses Gefaengnis.

    Die beiden Ports sind Vorgaben und keine Wahlmoeglichkeit des Kaefigs: sie
    kommen aus der Konfiguration des Cores und werden hier in Zahlen gegossen,
    bevor das Profil geschrieben wird.
    """
    return _PROFILE.format(jail=os.path.realpath(jail),
                           python_root=os.path.realpath(python_root),
                           broker_port=int(broker_port),
                           gateway_port=int(gateway_port))


def child_environment(*, jail: str, hermes_home: str) -> dict[str, str]:
    """Die Umgebung des Kindes — aus dem Nichts gebaut, nicht gefiltert."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": jail,
        "TMPDIR": jail,
        "LANG": "C.UTF-8",
        "HERMES_HOME": hermes_home,
    }


def leaking_names(env: dict[str, str]) -> list[str]:
    """Meldet verbotene Namen in einer Kindumgebung — gross/klein egal.

    Die Schreibweise ist nicht egal, weil jemand schludert: `Settings` liest mit
    `case_sensitive=False`, also ist `openai_api_key` derselbe Schluessel wie
    `OPENAI_API_KEY`. Eine Pruefung, die nur die Grossschreibung kennt, faende
    die halbe Wahrheit.
    """
    lowered = {k.lower() for k in env}
    return sorted(n for n in FORBIDDEN_ENV if n.lower() in lowered)


@dataclass
class SandboxedProcess:
    """Ein laufendes Kind samt der Grenze, unter der es laeuft."""

    process: Any
    jail: str
    profile_path: str

    @property
    def pid(self) -> int:
        return int(getattr(self.process, "pid", 0) or 0)

    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def stop(self, *, grace: float = 5.0) -> int | None:
        """Erst hoeflich, dann bestimmt. Ein haengendes Kind bleibt nicht stehen."""
        if not self.alive():
            return self.process.returncode if self.process else None
        self.process.terminate()
        try:
            return await asyncio.wait_for(self.process.wait(), timeout=grace)
        except (TimeoutError, asyncio.TimeoutError):
            self.process.kill()
            return await self.process.wait()


class SandboxUnavailable(RuntimeError):
    """Es gibt keine echte Isolation — dann gibt es auch keinen Executor."""


async def launch(argv: list[str], *, jail: str, hermes_home: str,
                 python_root: str, log_path: str,
                 broker_port: int = BROKER_PORT,
                 gateway_port: int = GATEWAY_PORT) -> SandboxedProcess:
    """Startet ein Kind unter dem Profil. Fail-closed an jeder Stelle.

    Kein Halbzustand und kein „notfalls eben ohne Sandbox": fehlt
    `sandbox-exec`, gibt es keinen tiefen Executor. Eine Aufgabe, die dann bei
    `executor_unavailable` stehen bleibt, ist die ehrlichere Lage als eine, die
    unbeaufsichtigt auf dem Rechner des Nutzers laeuft.
    """
    if not os.path.exists(SANDBOX_EXEC):
        raise SandboxUnavailable("sandbox-exec missing")
    if shutil.which(argv[0]) is None and not os.path.exists(argv[0]):
        raise SandboxUnavailable(f"executor binary missing: {os.path.basename(argv[0])}")

    os.makedirs(jail, mode=0o700, exist_ok=True)
    profile_path = os.path.join(jail, "sandbox.sb")
    with open(profile_path, "w", encoding="utf-8") as handle:
        handle.write(render_profile(jail=jail, python_root=python_root,
                                    broker_port=broker_port,
                                    gateway_port=gateway_port))

    env = child_environment(jail=jail, hermes_home=hermes_home)
    leaking = leaking_names(env)
    if leaking:
        # Kein `assert`: unter `python -O` waere die Pruefung weg — und ein
        # Geheimnis genau dort, wo es am meisten schadet.
        raise SandboxUnavailable(f"child environment carries {len(leaking)} forbidden names")

    handle = open(log_path, "ab", buffering=0)  # noqa: SIM115 - lebt so lang wie das Kind
    process = await asyncio.create_subprocess_exec(
        SANDBOX_EXEC, "-f", profile_path, *argv,
        cwd=jail, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=handle, stderr=handle)
    log.info("deep.executor_launched", pid=process.pid, sandbox="seatbelt",
             egress=f"443/80/53+broker:{broker_port}", bind=f"localhost:{gateway_port}")
    return SandboxedProcess(process=process, jail=jail, profile_path=profile_path)
