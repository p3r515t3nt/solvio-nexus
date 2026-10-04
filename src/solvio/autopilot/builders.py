"""Builder — ein Protokoll, mehrere Anbieter, genau ein realer Schreiber.

Die Abstraktion ist der Punkt (Amendment 3). Der Autopilot darf nicht an Codex
haengen, nur weil Codex heute der einzige ist, der schreiben darf. Deshalb
kennt der Rest der Maschine ausschliesslich `BuilderAdapter` — und das
Hinzufuegen eines sicheren Claude-Writers spaeter aendert **nur** diese Datei,
nicht die Zustandsmaschine, nicht das Ledger, nicht Handoff, Evidence,
Capacity, Failover oder das Context Package.

**Warum Claude hier gesperrt steht (Amendment 2).** Gemessen am 2026-08-29 am
installierten Claude Code: das CLI liest seine eigene Abo-Sitzung, indem es
`/usr/bin/security` als Unterprozess startet. Versiegelt laeuft es deshalb
nicht; unversiegelt truege **jedes** modellgesteuerte Kind dieselbe Sitzung
(gemessen: 510 Byte wiederverwendbares Anmeldematerial). Die Hoffnung auf die
binaergebundene ACL traegt nicht — beide Wege laufen durch dasselbe
`security`, dem die ACL vertraut. Das ist keine Vorsicht, sondern ein
gerissenes Gate mit definiertem Rueckfall.

`SyntheticBuilder` existiert nur, um die Failover-MECHANIK zu beweisen
(Amendment 7). Er schreibt eine Datei und traegt einen erzwingbaren
Quota-Ausgang; er ist ausdruecklich **kein** zweiter realer Writer, und der
Abschlussbericht muss das unterscheiden.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from solvio.logging_setup import get_logger
from solvio import git_binary as _GB

log = get_logger("autopilot")

# -- Was ein Adapter darf -----------------------------------------------------
WRITER = "WRITER"
BLOCKED_BY_SECURITY_POLICY = "BLOCKED_BY_SECURITY_POLICY"
CAPABILITIES = frozenset({WRITER, BLOCKED_BY_SECURITY_POLICY})

#: Aufgabengroessen. Grob und ehrlich — keine Tokenprognose.
SMALL, MEDIUM, LARGE = "SMALL", "MEDIUM", "LARGE"
TASK_SIZES = (SMALL, MEDIUM, LARGE)

#: Zeitdeckel je Groesse. Startwerte, ausdruecklich am Gemessenen zu
#: kalibrieren — dieselbe Prozedur wie bei den Broker-Kappen.
TIMEOUTS = {SMALL: 600.0, MEDIUM: 1800.0, LARGE: 3600.0}

#: Ausgaenge eines Bauschritts. Geschlossen.
OK = "ok"
FAILED = "failed"
QUOTA = "quota"
UNAVAILABLE = "unavailable"
REFUSED = "refused"
OUTCOMES = frozenset({OK, FAILED, QUOTA, UNAVAILABLE, REFUSED})

CHECKPOINT_PREFIX = "autopilot-checkpoint"


class BuilderError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class BuildTask:
    """Was ein Builder tun soll. Der Auftrag, nicht das Gespraech."""

    milestone_id: str
    instruction: str
    size: str = MEDIUM
    model: str = ""
    context: str = ""

    def prompt(self) -> str:
        teile = [self.context.strip(), self.instruction.strip()]
        return "\n\n".join(t for t in teile if t)


@dataclass
class BuildOutcome:
    """Was dabei herauskam — gemessen, wo es geht."""

    outcome: str
    builder: str
    text: str = ""
    elapsed: float = 0.0
    exit_code: int = 0
    pgid: int = 0
    started_at: float = 0.0
    executable: str = ""
    detail: str = ""
    provider_tokens: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    @property
    def quota(self) -> bool:
        return self.outcome == QUOTA

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "builder": self.builder,
                "elapsed": round(self.elapsed, 1), "exit_code": self.exit_code,
                "detail": self.detail[:300],
                "provider_tokens": self.provider_tokens}


class BuilderAdapter(Protocol):
    """Das Protokoll. Alles andere in der Maschine kennt nur das hier."""

    name: str

    def capability(self) -> str: ...
    def blocked_reason(self) -> str: ...
    async def status(self) -> dict[str, Any]: ...
    async def build(self, task: BuildTask, workdir: str) -> BuildOutcome: ...


# -- Checkpoint-Sicherheit (Amendment 6) --------------------------------------
def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
                "GIT_AUTHOR_NAME": "SOLVIO Autopilot",
                "GIT_AUTHOR_EMAIL": "autopilot@solvio.local",
                "GIT_COMMITTER_NAME": "SOLVIO Autopilot",
                "GIT_COMMITTER_EMAIL": "autopilot@solvio.local"})
    proc = subprocess.run([_GB.resolve(), "-C", repo, *args], env=env, timeout=300,
                          capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise BuilderError("git_failed",
                           f"{args[0]}: {proc.stderr.strip()[:200]}")
    return proc


@dataclass
class CheckpointResult:
    """Was ein Checkpoint getan hat — oder warum er es nicht durfte."""

    ok: bool
    commit: str = ""
    changed: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    reason: str = ""
    nothing_to_do: bool = False


def safe_checkpoint(repo: str, *, phase: str,
                    scanner=None) -> CheckpointResult:
    """Ein WIP-Commit — aber niemals blind (Amendment 6).

    Die Reihenfolge ist die ganze Sicherheit:

    1. **Vormerken mit `git add -A`.** Das respektiert `.gitignore`; was dort
       ausgeschlossen ist, kommt gar nicht erst in die Auswahl.
    2. **Pruefen, was vorgemerkt IST** — nicht, was der Baum enthaelt. Ein Scan
       ueber das ganze Repository findet „Kredentialgestalt" in jedem Dokument,
       das ueber Schluessel schreibt; die Agent Runtime hat das teuer gelernt
       (139 Treffer, Ernte haette immer verweigert). Die Frage ist: *hat dieser
       Schritt Anmeldematerial hineingelegt?*
    3. **Bei Fund: zuruecknehmen und fail closed.** Nicht committen, nicht
       harvesten, nicht an den naechsten Builder weiterreichen.

    Ohne diese Reihenfolge waere der Checkpoint der bequemste Weg, ein
    Geheimnis in die Geschichte und damit in jeden spaeteren Context zu
    bekommen.
    """
    if not os.path.isdir(os.path.join(repo, ".git")):
        return CheckpointResult(ok=False, reason="not_a_repository")

    _git(repo, "add", "-A")
    vorgemerkt = [z.strip() for z in
                  _git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
                  if z.strip()]
    if not vorgemerkt:
        return CheckpointResult(ok=True, nothing_to_do=True,
                                commit=_git(repo, "rev-parse", "HEAD").stdout.strip())

    befunde = list(_scan_staged(repo, vorgemerkt))
    if scanner is not None:
        befunde += list(scanner(repo, vorgemerkt) or [])
    if befunde:
        # Zuruecknehmen, damit der naechste Schritt nicht auf einer vorgemerkten
        # Kredentialdatei aufsetzt. Die Datei selbst bleibt liegen — sie zu
        # loeschen waere eine Aktion, die der Autopilot nicht schuldet.
        _git(repo, "reset", "--quiet", "HEAD", check=False)
        log.error("autopilot.checkpoint_refused", count=len(befunde))
        return CheckpointResult(ok=False, reason="credential_in_checkpoint",
                                findings=tuple(befunde[:20]),
                                changed=tuple(vorgemerkt[:50]))

    _git(repo, "commit", "--quiet", "-m",
         f"{CHECKPOINT_PREFIX}: {phase}")
    stand = _git(repo, "rev-parse", "HEAD").stdout.strip()
    log.info("autopilot.checkpoint", phase=phase, commit=stand[:12],
             files=len(vorgemerkt))
    return CheckpointResult(ok=True, commit=stand, changed=tuple(vorgemerkt[:200]))


#: Der leere Baum. Ein Wurzel-Commit hat keinen Vorgaenger — ohne diesen
#: Vergleichspunkt haette `git diff` nichts zu vergleichen und der Scan liefe
#: ins Leere, statt alles als neu zu lesen.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _diff_basis(bereich: str | None) -> list[str]:
    """Der Vergleichspunkt: der Index, oder ein Commit-Bereich.

    `--no-renames` gehoert hierher und nicht an die Aufrufstelle. Es ist der
    Riegel gegen die eine Verwechslung, die diesen Scan blind machen koennte:
    mit Umbenennungserkennung kann eine NEU angelegte Datei als „Umbenennung"
    einer geloeschten erscheinen — und ihr Inhalt damit als „stand schon da"
    gelten. Ohne die Heuristik ist sie schlicht `A`, und `A` wird ganz gelesen.
    """
    kopf = ["diff", "--cached"] if bereich is None else ["diff", bereich]
    return [*kopf, "--no-renames"]


def _change_status(repo: str, bereich: str | None) -> dict[str, str]:
    """Was git ueber jede Aenderung sagt: `A` neu, `M` geaendert, `D` fort."""
    proc = _git(repo, *_diff_basis(bereich), "--name-status", "-z", check=False)
    felder = [f for f in proc.stdout.split("\0") if f]
    lage: dict[str, str] = {}
    for i in range(0, len(felder) - 1, 2):
        lage[felder[i + 1]] = felder[i][:1]
    return lage


def _added_text(repo: str, rel: str, bereich: str | None) -> str:
    """Nur die Zeilen, die diese Aenderung HINZUFUEGT.

    Der Diff laeuft je Datei (`-- rel`). Das kostet einen Aufruf mehr und spart
    das Zerlegen von Pfaden aus dem Diff-Kopf — an dem sich sonst jeder Pfad
    mit Anfuehrungszeichen oder Umlaut rechtfertigen muesste.

    `+++` steht immer VOR dem ersten `@@`, also ausserhalb eines Blocks; eine
    Inhaltszeile, die selbst mit `+` beginnt, steht immer darin. Deshalb zaehlt
    hier der Blockzustand und nicht das Praefix allein.
    """
    proc = _git(repo, *_diff_basis(bereich), "-U0", "--no-color", "--", rel,
                check=False)
    zeilen: list[str] = []
    im_block = False
    for zeile in proc.stdout.splitlines():
        if zeile.startswith("@@"):
            im_block = True
            continue
        if not im_block:
            continue
        if zeile.startswith("+"):
            zeilen.append(zeile[1:])
        elif not zeile.startswith(("-", "\\")):
            im_block = False
    return "\n".join(zeilen)


def _scan_staged(repo: str, paths: list[str], *, bereich: str | None = None):
    """Name und Inhalt der AENDERUNG — nicht der ganzen Datei (DEBT-0189).

    Frueher las dieser Scan jede vorgemerkte Datei ganz. Damit beantwortete er
    eine andere Frage als die gestellte: nicht „hat dieser Schritt
    Anmeldematerial hineingelegt?", sondern „steht irgendwo in dieser Datei
    etwas Kredentialfoermiges?". Gemessen waren dadurch 25 von 474 verfolgten
    Dateien fuer den Autopiloten unberuehrbar — darunter Suiten, die
    Wegwerf-Werte ALS TESTDATEN tragen muessen. Eine Bauphase endete daran,
    ohne dass der Builder eine einzige kredentialfoermige Zeile geschrieben
    hatte.

    Die Unterscheidung ist jetzt die des Repositories selbst:

      * `M` — die Datei ist bekannt und geprueft. Gelesen wird, was HINZUKAM.
      * alles andere — `A`, eine Kopie, oder ein Pfad, ueber den git nichts
        sagt: der ganze Inhalt gilt als neu und wird ganz gelesen.

    Der unbekannte Fall faellt bewusst auf die strenge Seite. Wer den Scan
    umgehen wollte, muesste git dazu bringen, eine Aenderung zu verschweigen —
    und selbst dann liest er die Datei ganz.

    Die Namens- und Pfadpruefung bleibt unbedingt. Ein `.env` ist auch dann
    keins, wenn es bloss geaendert wurde; und ein Muster abzuschwaechen war
    nie die Aufgabe.
    """
    from solvio.agent_runtime import workspace as W
    from solvio.agent_runtime.specialists import redact_specialist_output

    lage = _change_status(repo, bereich)
    gelesen = 0
    for rel in paths:
        klein = rel.lower()
        if os.path.basename(klein) in W.FORBIDDEN_BASENAMES:
            yield f"name:{rel}"
            continue
        if any(teil in W.FORBIDDEN_PARTS for teil in klein.split("/")):
            yield f"path:{rel}"
            continue

        if lage.get(rel) == "M":
            inhalt = _added_text(repo, rel, bereich)
            groesse = len(inhalt.encode("utf-8", "surrogateescape"))
            if groesse > W.MAX_BLOB_BYTES or gelesen > W.MAX_SCANNED_BYTES:
                # Ehrlich gedeckelt: was darueber liegt, gilt NICHT als geprueft.
                yield f"unscanned:{rel}"
                continue
            gelesen += groesse
        else:
            voll = os.path.join(repo, rel)
            try:
                groesse = os.path.getsize(voll)
            except OSError:
                continue
            if groesse > W.MAX_BLOB_BYTES or gelesen > W.MAX_SCANNED_BYTES:
                yield f"unscanned:{rel}"
                continue
            gelesen += groesse
            try:
                with open(voll, encoding="utf-8", errors="strict") as fh:
                    inhalt = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

        if redact_specialist_output(inhalt) != inhalt:
            yield f"content:{rel}"


# -- Codex: der einzige reale Schreiber ---------------------------------------
class CodexBuilder:
    """Codex im nativen Sandkasten, Netz gepinnt aus.

    Die Invocation kommt unveraendert aus der Agent Runtime — sie ist dort
    gemessen (`curl` scheitert mit rc=7 gegen eine direkte IP, es ist nicht
    bloss DNS) und live abgenommen. Eine zweite Fassung derselben Argumente
    waere die erste, die jemand nachzuschaerfen vergisst.
    """

    name = "codex"

    def capability(self) -> str:
        return WRITER

    def blocked_reason(self) -> str:
        return ""

    async def status(self) -> dict[str, Any]:
        from solvio.specialists import providers as P
        lage = await P.codex_status()
        return lage.as_dict() | {"available": lage.available,
                                 "reason": lage.reason}

    async def build(self, task: BuildTask, workdir: str) -> BuildOutcome:
        from solvio.agent_runtime import specialists as SP
        from solvio.specialists import providers as P
        from solvio.specialists.launcher import LauncherError, run

        begonnen = time.time()
        try:
            aufruf = SP.codex_builder_invocation(
                workdir=workdir, model=task.model,
                timeout=TIMEOUTS.get(task.size, TIMEOUTS[MEDIUM]))
        except LauncherError as exc:
            return BuildOutcome(UNAVAILABLE, self.name, detail=exc.reason,
                                elapsed=time.time() - begonnen)
        ergebnis = await run(aufruf, task.prompt())
        text = P.codex_text(ergebnis)
        vergangen = time.time() - begonnen

        if P.quota_exhausted(text) or P.quota_exhausted(ergebnis.reason or ""):
            # Kontingent ist kein Fehlversuch. Diese Unterscheidung ist der
            # Grund, warum der Failover nicht als Reparaturschleife zaehlt.
            return BuildOutcome(QUOTA, self.name, text=SP.redact_specialist_output(text),
                                elapsed=vergangen, detail="provider_quota",
                                pgid=getattr(ergebnis, "pgid", 0),
                                started_at=getattr(ergebnis, "started_at", 0.0),
                                executable=getattr(ergebnis, "executable", ""))
        if not ergebnis.ok:
            return BuildOutcome(FAILED, self.name,
                                text=SP.redact_specialist_output(text),
                                elapsed=vergangen,
                                detail=(ergebnis.reason or "")[:200],
                                pgid=getattr(ergebnis, "pgid", 0),
                                started_at=getattr(ergebnis, "started_at", 0.0),
                                executable=getattr(ergebnis, "executable", ""))
        return BuildOutcome(OK, self.name, text=SP.redact_specialist_output(text),
                            elapsed=vergangen,
                            pgid=getattr(ergebnis, "pgid", 0),
                            started_at=getattr(ergebnis, "started_at", 0.0),
                            executable=getattr(ergebnis, "executable", ""))


# -- Claude: gebaut, gemessen, gesperrt ---------------------------------------
class ClaudeBuilder:
    """Der Platzhalter, der die Abstraktion ehrlich haelt.

    Er existiert, damit `builders.py` nicht heimlich „Codex" heisst. Sein
    `build()` wirft nicht und laeuft nicht — es gibt `REFUSED` zurueck, mit dem
    gemessenen Grund. Ein Adapter, der bei Sperre eine Ausnahme wirft, wuerde
    im Failover wie eine Stoerung aussehen; er ist aber eine Entscheidung.
    """

    name = "claude"
    REASON = "keychain_gate_failed:cli_spawns_security_subprocess"

    def capability(self) -> str:
        return BLOCKED_BY_SECURITY_POLICY

    def blocked_reason(self) -> str:
        return self.REASON

    async def status(self) -> dict[str, Any]:
        from solvio.specialists import providers as P
        lage = await P.claude_status()
        # Angemeldet oder nicht — schreiben darf er trotzdem nicht.
        return lage.as_dict() | {"available": False,
                                 "reason": self.REASON,
                                 "logged_in": lage.available}

    async def build(self, task: BuildTask, workdir: str) -> BuildOutcome:
        return BuildOutcome(REFUSED, self.name, detail=self.REASON)


# -- Claude am Broker: der echte Schreiber (V0.6) -----------------------------
#: Der Auftraggeber je Groesse. Der Auftraggeber IST der Zugang — das grosse
#: Modell erreicht nur, wer den Eskalations-Token haelt, und den praegt
#: ausschliesslich Core-Code.
WRITER_PRINCIPAL = "autopilot-writer-claude"
WRITER_ESCALATION_PRINCIPAL = "autopilot-writer-claude-escalation"


class ClaudeWriterBuilder:
    """Claude Code als echter Schreiber — gemakelt, versiegelt, ohne Anmeldung.

    Vier Riegel, jeder faengt einen Fall, den nur er faengt:

    1. `--bare` liest Schluesselbund und OAuth gar nicht (gemessen).
    2. Der Kaefig verbietet `/usr/bin/security` und JEDES Netz ausser der
       Rueckschleife zum Broker — kernel-erzwungen, auch fuer Kindprozesse.
    3. Das Token in der Kindumgebung ist ein **Broker**-Token: ohne offenes
       Lease `403`, nach dem Lease-Schluss `401`, ausserhalb der Rueckschleife
       wertlos.
    4. Die echte Anmeldung liegt im Tresor und wird nur im Broker-Ausgang
       eingesetzt — in einem Modul, das dieser Adapter nicht einmal importiert.

    Und ein fuenfter, der die anderen vier ueberwacht: der **Kanarienvogel**.
    Er misst bei jedem Gate-Lauf nach, ob `--bare` noch das tut, was gemessen
    wurde. Rot heisst gesperrt, nicht gewarnt.
    """

    name = "claude"

    def __init__(self, *, broker: Any = None, port: int = 0,
                 canary_verdict: str | None = None) -> None:
        self.broker = broker
        self.port = int(port or 0)
        #: Ein bereits gefaelltes Kanarienvogel-Urteil. `None` heisst: bei
        #: Bedarf selbst messen (und das Ergebnis merken — ein Dauertest je
        #: Bauphase waere eine Minute Wartezeit ohne neuen Erkenntniswert).
        self._canary = canary_verdict
        self._lease = ""

    # -- Zustand -------------------------------------------------------------

    def capability(self) -> str:
        """Gesperrt heisst hier **Sicherheitsrichtlinie**, nicht „gerade nichts da".

        Die Unterscheidung ist keine Wortklauberei: ein roter Kanarienvogel
        sagt, dass die CLI die Anmeldung anders behandelt als gemessen — das
        ist eine Richtlinienfrage, und der Adapter darf gar nicht laufen. Eine
        fehlende Anmeldung dagegen ist eine LAGE: derselbe Adapter ist morgen
        brauchbar, sobald der Eigentuemer sie eingespielt hat. Sie meldet sich
        deshalb ueber `status()` als `UNAVAILABLE` mit Grund, nicht als Sperre.
        """
        return BLOCKED_BY_SECURITY_POLICY if self._policy_block() else WRITER

    def _policy_block(self) -> str:
        """Nur der Kanarienvogel. Gemessen, gecached, rot heisst gesperrt."""
        if self._canary is None:
            from solvio.autopilot import canary as CANARY
            self._canary = CANARY.verdict(CANARY.run())
        return self._canary

    def blocked_reason(self) -> str:
        """Der EINE Grund, warum dieser Schreiber gerade nicht baut.

        Reihenfolge mit Absicht: erst der Kanarienvogel (eine CLI, die die
        Anmeldung anders behandelt als gemessen, macht jede weitere Pruefung
        gegenstandslos), dann die Anmeldung selbst.
        """
        sperre = self._policy_block()
        if sperre:
            return sperre
        if not self._credential_present():
            return "no_credential"
        return ""

    def _credential_present(self) -> bool:
        """Ob im Tresor ueberhaupt eine Anthropic-Anmeldung liegt.

        Geprueft wird die **Existenz**, nie der Wert — `exists()` oeffnet
        nichts. Dieser Adapter darf wissen, DASS es sie gibt; was sie ist,
        erfaehrt er nie.
        """
        try:
            from solvio.provider_broker import anthropic as AN
            from solvio.secret_vault.broker import SecretBroker
            return bool(SecretBroker().exists(AN.SECRET_REF))
        except Exception:  # noqa: BLE001 - ein kaputter Tresor ist kein Absturz
            return False

    async def status(self) -> dict[str, Any]:
        from solvio.specialists import providers as P
        lage = await P.claude_status()
        grund = self.blocked_reason()
        return lage.as_dict() | {"available": not grund,
                                 "reason": grund,
                                 "logged_in": lage.available}

    # -- Der Bau -------------------------------------------------------------

    def _principal(self, size: str) -> str:
        return WRITER_ESCALATION_PRINCIPAL if size == LARGE else WRITER_PRINCIPAL

    def _model(self, task: BuildTask) -> str:
        from solvio.provider_broker import anthropic as AN
        if task.size == LARGE:
            return AN.WRITER_LARGE_MODEL
        return AN.WRITER_MODEL

    def _token(self, principal: str) -> str:
        """Je Bauphase frisch. Der Broker rotiert beim Lease-Schluss.

        Dieselbe Lehre wie beim Technical Lead, wo ein gemerkter Token den
        zweiten Aufruf 45 Minuten spaeter an einem 401 sterben liess.
        """
        from solvio.autopilot import lead as LEAD
        if self.broker is not None:
            return self.broker.register_principal(principal)
        return LEAD.token_from_core(principal)

    def _open_lease(self, principal: str) -> str:
        from solvio.autopilot import lead as LEAD
        if self.broker is None:
            return LEAD.lease_from_core(principal)
        try:
            return self.broker.open_lease(
                principal, ref=f"autopilot:{principal}",
                deadline=time.time() + 3600.0)
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("autopilot.writer_lease_refused", principal=principal,
                     kind=type(exc).__name__)
            return ""

    def _close_lease(self, principal: str, lease: str) -> None:
        if not lease:
            return
        from solvio.autopilot import lead as LEAD
        try:
            if self.broker is None:
                LEAD.close_lease_in_core(principal, lease)
            else:
                self.broker.close_lease(lease)
        except Exception:  # noqa: BLE001 - steht in einem finally
            pass

    async def build(self, task: BuildTask, workdir: str) -> BuildOutcome:
        from solvio.agent_runtime import isolation, specialists as SP
        from solvio.specialists.launcher import (LauncherError,
                                                 brokered_environment)

        begonnen = time.time()
        grund = self.blocked_reason()
        if grund:
            # Eine Sperre ist eine Entscheidung, keine Stoerung: `REFUSED`
            # sieht im Failover anders aus als ein Fehlschlag.
            return BuildOutcome(REFUSED, self.name, detail=grund,
                                elapsed=time.time() - begonnen)

        port = self.port or _broker_port()
        principal = self._principal(task.size)
        # Reihenfolge: erst Token, DANN Lease. Ein Lease auf einen
        # unregistrierten Auftraggeber wird abgewiesen — live gemessen.
        token = self._token(principal)
        if not token:
            return BuildOutcome(UNAVAILABLE, self.name, detail="no_broker_token",
                                elapsed=time.time() - begonnen)
        lease = self._open_lease(principal)
        if not lease:
            return BuildOutcome(QUOTA, self.name, detail="lease_refused",
                                elapsed=time.time() - begonnen)

        try:
            # Der Kaefig liegt AUSSERHALB des Arbeitsbereichs.
            #
            # Live gefunden in der B5-Abnahme (DEBT-0184): lag er unter
            # `<workspace>/.autopilot/jail`, nahm `safe_checkpoint` ihn mit —
            # `git add -A` sieht alles, was nicht in `.gitignore` steht, und
            # das gerenderte Seatbelt-Profil landete im Checkpoint, im
            # veroeffentlichten Ref und damit in jeder Offsite-Generation.
            #
            # Eine Ignore-Regel waere die schwaechere Antwort: sie versteckt
            # etwas, das im Baum liegt, und die naechste Regel versteckt dann
            # vielleicht ein echtes Ergebnis. Ein Laufzeitartefakt, das nie in
            # den Baum kommt, braucht keine Regel.
            jail = _jail_dir(workdir)
            laufzeit = os.path.join(jail, "tmp")
            os.makedirs(laufzeit, mode=0o700, exist_ok=True)
            umgebung = brokered_environment(
                base_url=f"http://127.0.0.1:{port}", token=token,
                tmpdir=laufzeit)
            argv = SP.claude_brokered_argv(workdir=workdir,
                                           model=self._model(task))
        except LauncherError as exc:
            self._close_lease(principal, lease)
            return BuildOutcome(UNAVAILABLE, self.name, detail=exc.reason,
                                elapsed=time.time() - begonnen)

        try:
            prozess, kind = await isolation.launch(
                argv, workspace=workdir, scratch=jail, env=umgebung,
                tool_state=(), broker_port=port)
        except isolation.BuilderJailUnavailable as exc:
            self._close_lease(principal, lease)
            return BuildOutcome(UNAVAILABLE, self.name, detail=str(exc)[:200],
                                elapsed=time.time() - begonnen)

        frist = TIMEOUTS.get(task.size, TIMEOUTS[MEDIUM])
        try:
            roh_out, roh_err = await asyncio.wait_for(
                prozess.communicate(task.prompt().encode()), timeout=frist)
            code = prozess.returncode
        except asyncio.TimeoutError:
            _kill(kind)
            self._close_lease(principal, lease)
            return BuildOutcome(FAILED, self.name, detail="timeout",
                                elapsed=time.time() - begonnen,
                                pgid=kind.pgid, started_at=kind.started_at,
                                executable=kind.executable)
        finally:
            self._close_lease(principal, lease)

        text = SP.redact_specialist_output(
            (roh_out or b"").decode("utf-8", "replace"))
        fehlertext = SP.redact_specialist_output(
            (roh_err or b"").decode("utf-8", "replace"))
        vergangen = time.time() - begonnen
        gemeinsam = {"pgid": kind.pgid, "started_at": kind.started_at,
                     "executable": kind.executable, "elapsed": vergangen}

        knapp = _quota_signal(text, fehlertext)
        if knapp:
            # Kontingent ist KEIN Reparaturversuch (Vertrag §7).
            return BuildOutcome(QUOTA, self.name, text=text,
                                detail=knapp, **gemeinsam)
        if _credential_signal(text, fehlertext):
            return BuildOutcome(UNAVAILABLE, self.name, text=text,
                                detail="no_credential", **gemeinsam)
        if code != 0:
            return BuildOutcome(FAILED, self.name, text=text,
                                detail=(fehlertext or f"exit_{code}")[:200],
                                **gemeinsam)
        return BuildOutcome(OK, self.name, text=text, **gemeinsam)


def _jail_dir(workdir: str) -> str:
    """Ein Kaefigverzeichnis NEBEN dem Arbeitsbereich, nie darin.

    Der Name haengt am Arbeitsbereich, damit zwei Laeufe sich nicht ins
    Gehege kommen, und liegt im Temp des Systems — also dort, wo
    Laufzeitkram hingehoert und wo kein `git add` hinsieht.
    """
    import hashlib
    import tempfile
    fingerabdruck = hashlib.sha256(
        os.path.realpath(workdir).encode("utf-8")).hexdigest()[:12]
    pfad = os.path.join(tempfile.gettempdir(),
                        f"solvio-autopilot-jail-{fingerabdruck}")
    os.makedirs(pfad, mode=0o700, exist_ok=True)
    return pfad


def _broker_port() -> int:
    from solvio.provider_broker.service import configured_port
    return int(configured_port())


def _kill(kind: Any) -> None:
    try:
        if kind.pgid:
            os.killpg(kind.pgid, 9)
    except OSError:
        pass


#: Was der Broker sagt, wenn die Kappe steht — und was der Anbieter sagt, wenn
#: das Abo-Fenster zu ist. Beides ist Kontingent, keines ein Fehlversuch.
_QUOTA_MARKERS = ("token_capped", "rate_capped", "429",
                  "usage limit", "rate limit", "quota")

#: Was der Broker sagt, wenn gar keine Anmeldung im Tresor liegt.
_CREDENTIAL_MARKERS = ("no_credential", "credential_denied",
                       "credential_malformed", "vault_unavailable")


def _quota_signal(*texte: str) -> str:
    zusammen = " ".join(t or "" for t in texte).lower()
    for marke in _QUOTA_MARKERS:
        if marke in zusammen:
            return f"provider_quota:{marke}"
    return ""


def _credential_signal(*texte: str) -> bool:
    zusammen = " ".join(t or "" for t in texte).lower()
    return any(m in zusammen for m in _CREDENTIAL_MARKERS)


# -- Synthetisch: nur fuer den Beweis der Mechanik ----------------------------
class SyntheticBuilder:
    """Ein kontrollierter zweiter Adapter (Amendment 7).

    Er beweist, dass die Failover-Mechanik traegt — Checkpoint, Uebernahme,
    Fortsetzung —, und er beweist ausdruecklich **nicht**, dass ein zweiter
    realer Schreiber existiert. Der Abschlussbericht haelt beides auseinander.
    """

    name = "synthetic"

    def __init__(self, *, writes: dict[str, str] | None = None,
                 outcome: str = OK, detail: str = "") -> None:
        self.writes = dict(writes or {})
        self.outcome = outcome
        self.detail = detail
        self.calls = 0

    def capability(self) -> str:
        return WRITER

    def blocked_reason(self) -> str:
        return ""

    async def status(self) -> dict[str, Any]:
        return {"anbieter": self.name, "available": self.outcome != UNAVAILABLE,
                "reason": "" if self.outcome != UNAVAILABLE else "gestellt"}

    async def build(self, task: BuildTask, workdir: str) -> BuildOutcome:
        self.calls += 1
        if self.outcome in (QUOTA, UNAVAILABLE, FAILED):
            return BuildOutcome(self.outcome, self.name,
                                detail=self.detail or self.outcome, elapsed=0.01)
        for rel, inhalt in self.writes.items():
            ziel = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(ziel), exist_ok=True)
            with open(ziel, "w", encoding="utf-8") as fh:
                fh.write(inhalt)
        return BuildOutcome(OK, self.name, text=f"{len(self.writes)} Datei(en)",
                            elapsed=0.01)


# -- Die Registratur ----------------------------------------------------------
def default_adapters(*, brokered_claude: bool = True) -> dict[str, Any]:
    """Die bekannten Adapter. `claude` steht bewusst darin.

    Wer ihn weglaesst, kann spaeter nicht erklaeren, warum er fehlt; wer ihn
    mit seinem Grund fuehrt, hat den Grund an der Sache.

    Seit V0.6 ist er der **gemakelte** Schreiber: `--bare` am eigenen Broker,
    im Kaefig mit genau einem Loopback-Ziel. Ob er wirklich baut, entscheidet
    er selbst und ehrlich — ein roter Kanarienvogel sperrt ihn
    (`BLOCKED_BY_SECURITY_POLICY`), eine fehlende Anmeldung macht ihn
    `UNAVAILABLE`. `brokered_claude=False` gibt den alten, grundsaetzlich
    gesperrten Platzhalter zurueck; das ist der Rueckfall fuer einen Baum
    ohne Anthropic-Flaeche, nicht ein Schalter fuer den Betrieb.
    """
    claude = ClaudeWriterBuilder() if brokered_claude else ClaudeBuilder()
    return {"codex": CodexBuilder(), "claude": claude}


def writers(adapters: dict[str, Any]) -> list[str]:
    """Wer tatsaechlich schreiben DARF — nicht, wer gerade kann.

    `capability()` ist die Richtlinienfrage. Ob ein Schreiber gerade eine
    Anmeldung oder Kontingent hat, steht in `capacity.py` und gehoert nicht
    hierher: sonst hiesse „kein Schreiber vorhanden" morgen etwas anderes als
    heute, und der Bericht koennte nie sagen, welches von beidem fehlte.
    """
    return sorted(n for n, a in adapters.items() if a.capability() == WRITER)


def blocked_writers(adapters: dict[str, Any]) -> dict[str, str]:
    return {n: a.blocked_reason() for n, a in adapters.items()
            if a.capability() == BLOCKED_BY_SECURITY_POLICY}
