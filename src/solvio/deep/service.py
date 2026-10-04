"""Den tiefen Executor hochfahren — oder gar nicht.

Dieselbe Haltung wie beim Freigabeweg: kein Halbzustand. Fehlt das Gefaengnis,
fehlt `sandbox-exec`, fehlt der Anbieter-Schluessel, oder bietet der gestartete
Prozess mehr an als erlaubt — dann gibt es keinen tiefen Executor, und tiefe
Aufgaben bleiben bei `executor_unavailable` stehen. Das ist die ehrlichere Lage
als ein Agent, der unbeaufsichtigt auf dem Rechner des Nutzers laeuft.

Seit Provider Broker V1 erreicht **kein** Anbieter-Schluessel mehr das
Gefaengnis. In die `.env` wandert ein Broker-Token des Cores; er oeffnet keinen
Anbieter, sondern nur den Broker auf der Rueckschleife, und auch den nur mit
offenem Lease. Steht der Broker nicht, gibt es keinen tiefen Executor — dieselbe
fail-closed Haltung, die frueher fuer den fehlenden Anbieterschluessel galt.

Home Assistant, Kalender, Gmail, die Freigabedatenbank und das Gedaechtnis
bleiben aussen vor, doppelt: keine Anmeldedaten, und ueber das Netz nicht
erreichbar.

**Zum Gefaengnisschloss.** Ein Gefaengnis hat genau einen Bewohner, und wer das
an einer Prozesskennung in einer Datei festmacht, hat kein Schloss, sondern eine
Notiz. Das ist hier produktiv passiert: nach einem Neustart stand in
`executor.pid` die Kennung 1072 aus einem frueheren Core-Leben, der Kern gab
dieselbe Zahl an einen voellig unbeteiligten Systemdienst weiter, und
`os.kill(pid, 0)` konnte die beiden nicht auseinanderhalten. Deep galt damit als
**fuer immer** belegt; der Doktor versuchte es zweimal und gab auf. Seitdem
haelt ein echtes Schloss des Betriebssystems (`fcntl.flock`) den Besitz: es
verschwindet, wenn der besitzende Prozess stirbt, ganz gleich wie er stirbt, und
eine wiederverwendete Kennung kann keinen Besitz erzeugen. Die Kennung steht
weiter in der Datei — als **Diagnose fuer Menschen**, nicht als Besitztitel.
"""
from __future__ import annotations

import errno
import fcntl
import os
import secrets
import time
from typing import Any

from solvio.deep import executor as ex
from solvio.deep.hermes import HermesClient
from solvio.deep.isolation import SandboxUnavailable, interpreter_root
from solvio.deep.journal import DeepJournal
from solvio.deep.runtime import HermesDeepRuntime
from solvio.logging_setup import get_logger

log = get_logger("deep")

#: Der Name, unter dem das Gateway beim Broker gefuehrt wird. Die Generation
#: zaehlt je Auftraggeber — ein `doctor hermes_restart` praegt nur diesen neu.
BROKER_PRINCIPAL = "deep-gateway"

DEFAULT_PORT = 8791
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_PROVIDER = "openai-api"

#: Wie lange auf den Start des Executors gewartet wird, bevor er als nicht
#: erreichbar gilt.
START_TIMEOUT = 90.0


class DeepRuntimeService:
    """Haelt Journal, Executor-Prozess und Laufzeit zusammen."""

    def __init__(self, *, config: ex.ExecutorConfig, state_dir: str,
                 broker: Any = None) -> None:
        self.config = config
        self.state_dir = state_dir
        self.broker = broker
        self.journal: DeepJournal | None = None
        self.client: HermesClient | None = None
        self.runtime: HermesDeepRuntime | None = None
        self.process: Any = None
        self.toolsets: list[str] = []
        self.lock = JailLock(config.jail)

    async def start(self) -> HermesDeepRuntime:
        import asyncio

        # Fail-closed, an derselben Stelle wie frueher der fehlende
        # Anbieterschluessel: ohne lauschenden Broker kann der Kaefig keine
        # Inferenz kaufen, und dann wird auch keine `.env` geschrieben.
        if self.broker is None or not self.broker.listening():
            raise SandboxUnavailable("provider broker is not listening")

        self.journal = DeepJournal(os.path.join(self.state_dir, "deep_tasks.sqlite3"))
        await self.journal.open()

        self.lock.acquire()
        api_key = _api_key(self.config.jail)
        token = self.broker.register_principal(BROKER_PRINCIPAL)
        # Die Rotation bei Lease-Null schreibt genau diese Datei neu. Der
        # Broker praegt zuerst und meldet dann hierher — scheitert das
        # Schreiben, bleibt der alte Token ungueltig.
        self.broker.set_credential_writer(BROKER_PRINCIPAL, self._rewrite_credential)
        ex.provision(self.config, api_key=api_key, broker_token=token)
        self.process = await ex.start(self.config)
        self.client = HermesClient(base_url=self.config.base_url, api_key=api_key)

        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self.client.healthy():
                break
            if not self.process.alive():
                raise SandboxUnavailable("executor exited during startup")
            await asyncio.sleep(2.0)
        else:
            await self.stop()
            raise SandboxUnavailable("executor did not become healthy")

        # Die Selbstauskunft des laufenden Prozesses, nicht die Datei. Wirft,
        # wenn dort mehr steht als erlaubt.
        self.toolsets = await ex.assert_posture(self.client)

        self.runtime = HermesDeepRuntime(journal=self.journal, client=self.client,
                                         config=self.config, process=self.process,
                                         broker=self.broker)
        stale = await self.journal.unfinished()
        for row in stale:
            # Ein neu startender Mac setzt nichts fort, was er nicht mehr
            # ueberblickt. Offene Aufgaben werden ehrlich beendet, nicht
            # stillschweigend wiederbelebt.
            await self.runtime.cancel_task(row["task_id"])
        log.info("deep.service_started", port=self.config.port,
                 toolsets="+".join(self.toolsets), recovered=len(stale),
                 broker_port=self.config.broker_port,
                 paused=await self.journal.paused())
        return self.runtime

    def _rewrite_credential(self, token: str) -> None:
        """Der Broker hat neu gepraegt — die `.env` zieht nach.

        `provision` schreibt die Datei vollstaendig neu, also bleiben
        `API_SERVER_KEY`, Host und Port erhalten. Der angeheftete Hermes liest
        die Zugangsdaten je eingereichtem Lauf neu aus der **Datei** (er
        bevorzugt sie ausdruecklich vor der Prozessumgebung und verwirft seinen
        Merker bei geaenderter mtime) — deshalb braucht die Rotation keinen
        Neustart des Gateways.
        """
        api_key = _api_key(self.config.jail)
        ex.provision(self.config, api_key=api_key, broker_token=token)

    def healthy_enough(self) -> bool:
        """Bereitschaft heisst: Gateway UND Broker.

        Ohne den Broker meldete Deep „gesund", waehrend jede Inferenz auf einen
        toten Port liefe.
        """
        return bool(self.broker is not None and self.broker.listening())

    async def stop(self) -> None:
        self.lock.release()
        if self.process is not None:
            await self.process.stop()
            self.process = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.journal is not None:
            await self.journal.close()
            self.journal = None


#: Das echte Schloss. Der Besitz haengt am gehaltenen `flock` dieses
#: Dateideskriptors, NICHT an seinem Inhalt.
LOCK_FILE = "executor.lock"

#: Die alte Datei aus der Zeit vor dem echten Schloss. Sie hat nie Besitz
#: begruendet und tut es jetzt erst recht nicht; sie wird aufgeraeumt, sobald
#: das Schloss steht.
LEGACY_LOCK_FILE = "executor.pid"


class JailLock:
    """Besitz am Gefaengnis, vom Betriebssystem gehalten.

    Warum ueberhaupt ein Schloss: `provision` schreibt Konfiguration und Port in
    das Zuhause des Executors. Startete ein zweiter Dienst auf demselben
    Gefaengnis, ueberschriebe er dem ersten die Einstellung unter den Fuessen —
    und beide arbeiteten danach mit einer Datei, der keiner von beiden mehr
    glaubt.

    Warum `flock` und keine Prozesskennung: ein `flock` gehoert dem **offenen
    Deskriptor**. Stirbt der Prozess — sauber, per `SIGKILL`, im Absturz, beim
    Stromausfall —, schliesst der Kern den Deskriptor und der Besitz ist weg.
    Es gibt keinen Zustand, in dem ein toter Besitzer weiterbesitzt, und keine
    wiederverwendete Kennung, die einen Besitz erfaende. Genau das ist in der
    Produktion schiefgegangen.
    """

    def __init__(self, jail: str) -> None:
        self.jail = jail
        self.path = os.path.join(jail, LOCK_FILE)
        self._fd: int | None = None

    def acquire(self) -> None:
        os.makedirs(self.jail, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise SandboxUnavailable(
                    f"jail lock unavailable: {exc.errno}") from None
            note = _read_note(fd)
            os.close(fd)
            # Ein LEBENDER Besitzer. Das ist die Absage, die stehen bleiben
            # soll — im Gegensatz zu der von frueher, die nie verging.
            raise SandboxUnavailable(
                f"jail is held by a live owner ({note or 'unknown'})") from None
        self._fd = fd
        self._write_note()
        self._retire_legacy()

    def release(self) -> None:
        """Gibt den Besitz zurueck.

        Die Datei wird **nicht** geloescht: ein zweiter Prozess kann sie bereits
        geoeffnet haben und am Schloss warten; ein `unlink` verschoebe ihn auf
        eine Datei, die niemand mehr sieht, und beide haetten „das Schloss".
        Der Besitz endet mit dem Schliessen des Deskriptors, und das genuegt.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def held(self) -> bool:
        return self._fd is not None

    def _write_note(self) -> None:
        """Fuer Menschen, nicht fuer die Entscheidung."""
        if self._fd is None:
            return
        note = (f"pid={os.getpid()} since={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                "# Diese Zeile ist Diagnose. Der Besitz haengt am flock dieser\n"
                "# Datei, nicht an der Kennung — eine wiederverwendete Kennung\n"
                "# begruendet keinen Besitz.\n")
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, note.encode("utf-8"))
            os.fsync(self._fd)
        except OSError:
            pass

    def _retire_legacy(self) -> None:
        """Die alte Kennungsdatei verschwindet, sobald das Schloss steht.

        Sie stehen zu lassen hiesse, zwei Dinge im Gefaengnis zu haben, die
        „Besitzer" behaupten — und die naechste Sitzung liest die falsche.
        """
        legacy = os.path.join(self.jail, LEGACY_LOCK_FILE)
        try:
            if os.path.exists(legacy):
                os.remove(legacy)
                log.info("deep.legacy_lock_retired")
        except OSError:
            pass


def _read_note(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 200).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return raw.splitlines()[0] if raw else ""


def _api_key(jail: str) -> str:
    """Der Schluessel zwischen Core und Executor. Einmal erzeugt, dann bestaendig.

    Hermes verlangt mindestens 16 Zeichen und weigert sich sonst zu starten —
    dieselbe fail-closed Haltung, die SOLVIO an seinen eigenen Grenzen hat.
    """
    path = os.path.join(jail, "api_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if len(existing) >= 32:
            return existing
    key = secrets.token_hex(32)
    os.makedirs(jail, mode=0o700, exist_ok=True)
    previous = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    return key


def from_environment(settings: Any, broker: Any = None) -> DeepRuntimeService | None:
    """Baut den Dienst aus der Umgebung — oder gar nicht.

    Bewusst dieselbe Form wie `approver_runtime.from_environment`: fehlt eine
    Angabe, wird `None` geliefert und protokolliert, warum. Ein tiefer Executor
    ist eine Erweiterung, kein Startzwang — ohne ihn laeuft SOLVIO vollstaendig
    weiter, nur ohne Recherche.
    """
    jail = (os.environ.get("SOLVIO_DEEP_JAIL", "") or "").strip()
    state = (os.environ.get("SOLVIO_DEEP_STATE_DIR", "") or "").strip()
    if not (jail and state):
        log.info("deep.not_configured", jail=bool(jail), state=bool(state))
        return None
    if not os.path.isdir(jail):
        log.error("deep.jail_missing")
        return None
    venv_bin = os.path.join(jail, "venv", "bin")
    if not os.path.exists(os.path.join(venv_bin, "hermes")):
        log.error("deep.executor_not_installed")
        return None
    python_root = (os.environ.get("SOLVIO_DEEP_PYTHON_ROOT", "") or "").strip()
    if not python_root:
        python_root = interpreter_root(venv_bin)
    os.makedirs(state, mode=0o700, exist_ok=True)

    config = ex.ExecutorConfig(
        jail=jail, venv_bin=venv_bin, python_root=python_root,
        port=int(os.environ.get("SOLVIO_DEEP_PORT", DEFAULT_PORT)),
        model=(os.environ.get("SOLVIO_DEEP_MODEL", "") or DEFAULT_MODEL),
        provider=(os.environ.get("SOLVIO_DEEP_PROVIDER", "") or DEFAULT_PROVIDER),
        broker_port=broker_port(broker))
    return DeepRuntimeService(config=config, state_dir=state, broker=broker)


def broker_port(broker: Any) -> int:
    """Der Port, den der Kaefig erreichen darf — vom laufenden Broker, nicht
    aus einer zweiten Quelle, die auseinanderlaufen kann."""
    port = int(getattr(broker, "port", 0) or 0)
    return port or isolation_broker_default()


def isolation_broker_default() -> int:
    from solvio.deep import isolation

    return isolation.BROKER_PORT
