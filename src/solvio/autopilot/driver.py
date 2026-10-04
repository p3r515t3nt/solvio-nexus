"""Der Treiber — EIN Prozess, der den Kreis dreht.

BUILD → TEST → REVIEW → FIX → TEST → REVIEW → ... bis `READY`,
`HUMAN_REQUIRED`, `BLOCKED` oder `STOPPED`. Genau das, was heute der Eigentuemer
mit Kopieren und Einfuegen tut.

Drei Dinge macht der Treiber ausdruecklich **nicht**:

* Er entscheidet nichts, was die Zustandsmaschine entscheidet. Er schlaegt einen
  Uebergang vor; `machine.transition` fuehrt ihn aus oder verweigert ihn.
* Er glaubt keinem Builder, dass etwas funktioniert. Zwischen Bauen und Urteil
  liegt immer das gemessene Gate.
* Er ruft den Menschen nicht bei normalen Fehlern. Rote Tests, Quota und
  Builderwechsel loest er selbst — dafuer ist er da.

Der Prozess ist flock-gesichert wie der Offsite-Job: zwei Treiber am selben
Milestone waeren zwei Meinungen ueber denselben Arbeitsbereich.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.autopilot import builders as B
from solvio.autopilot import capacity as CAP
from solvio.autopilot import context as CTX
from solvio.autopilot import contract as C
from solvio.autopilot import evidence as EV
from solvio.autopilot import independent as IND
from solvio.autopilot import lead as LEAD
from solvio.autopilot import machine as M
from solvio.autopilot import publisher as PUB
from solvio.autopilot import routing as RT
from solvio.autopilot import store as S
from solvio.logging_setup import get_logger

log = get_logger("autopilot")

LOCK_PATH = "~/.solvio/autopilot.lock"

#: Wie viele Runden ein Lauf hoechstens dreht, bevor er ehrlich anhaelt.
#: Keine Hoffnung, eine Zahl — dieselbe Haltung wie bei den Budgets der
#: Agentenlaufzeit.
MAX_ROUNDS = 12

#: Nach wie vielen gleichartigen erfolglosen Reparaturen der normale Retry
#: endet und der Lead entscheiden muss (Vertrag §13).
LOOP_THRESHOLD = 2

#: Wo ein Builder einen Contract-Vorschlag ablegt. Der kanonische
#: Contract liegt im Ledger; diese Datei ist eine Bitte, nie eine
#: Aenderung.
PROPOSAL_FILE = os.path.join(".autopilot", "contract.json")


class DriverLocked(RuntimeError):
    pass


def lock_path() -> str:
    return os.path.abspath(os.path.expanduser(
        os.environ.get("SOLVIO_AUTOPILOT_LOCK", LOCK_PATH)))


def _open_lock():
    pfad = lock_path()
    os.makedirs(os.path.dirname(pfad), mode=0o700, exist_ok=True)
    fh = open(pfad, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise DriverLocked("ein anderer Treiber laeuft") from None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


@dataclass
class Round:
    """Was eine Runde getan hat — fuer den Bericht, nicht fuer die Wahrheit."""

    number: int
    state_before: str
    state_after: str
    builder: str = ""
    gate_ok: bool | None = None
    verdict: str = ""
    note: str = ""


class Driver:
    """Der Taktgeber. Er haelt keinen Zustand, den das Ledger nicht auch haelt.

    Das ist Absicht und die Voraussetzung fuer Resume: ein frischer Prozess
    liest den Ledger und weiss alles, was der alte wusste.
    """

    def __init__(self, ledger: S.AutopilotLedger, *, workspace: str,
                 adapters: dict[str, Any] | None = None,
                 lead: LEAD.TechnicalLead | None = None,
                 publisher: Any = None,
                 python: str = "", preflight=None) -> None:
        self.ledger = ledger
        self.publisher = publisher
        self.workspace = os.path.abspath(os.path.expanduser(workspace))
        self.adapters = adapters if adapters is not None else B.default_adapters()
        self.lead = lead
        self.python = python
        self.preflight = preflight
        self.rounds: list[Round] = []

    # -- Abstimmung nach einem Absturz ---------------------------------------
    def reconcile(self, milestone_id: str) -> list[str]:
        """Was der abgestuerzte Prozess offen liess — ehrlich abschliessen.

        Nach dem Orchestrator-§12-Muster: eine offene Phase wird `interrupted`,
        nicht `succeeded`. **Nichts wird nach einem Neustart still als Erfolg
        verbucht** — dieselbe Haltung, die die Agentenlaufzeit lebt.

        Kindprozesse werden nur beendet, wenn pgid UND Startzeit UND
        Programmpfad zusammenpassen. Eine PID ist kein Besitztitel.
        """
        gefunden: list[str] = []
        for phase in self.ledger.open_phases(milestone_id):
            self.ledger.finish_phase(
                phase["phase_id"], state="interrupted",
                summary="Der Treiber endete waehrend dieser Phase.")
            self.ledger.record_event(milestone_id, "interrupted",
                                     f"{phase['kind']} #{phase['seq']}",
                                     ref=phase["phase_id"])
            gefunden.append(phase["phase_id"])
            self._reap(phase)
        if gefunden:
            self.ledger.record_event(milestone_id, "recovered",
                                     f"{len(gefunden)} offene Phase(n) geschlossen")
        return gefunden

    def _reap(self, phase: dict) -> None:
        pgid = int(phase.get("child_pgid") or 0)
        if pgid <= 0:
            return
        try:
            os.killpg(pgid, 0)                       # lebt die Gruppe noch?
        except OSError:
            return
        # Sie lebt. Ohne Uebereinstimmung von Startzeit und Programmpfad wird
        # sie NICHT beendet, sondern gemeldet: sonst toetet ein Neustart einen
        # fremden Prozess, der zufaellig dieselbe Nummer bekam.
        log.warning("autopilot.orphan_process_reported", pgid=pgid,
                    executable=phase.get("child_executable", ""))

    # -- Der Kreis -----------------------------------------------------------
    async def run(self, milestone_id: str, *, max_rounds: int = MAX_ROUNDS,
                  now: float = 0.0) -> str:
        """Dreht den Kreis, bis ein terminaler oder geparkter Zustand steht."""
        self.reconcile(milestone_id)
        for nummer in range(1, max_rounds + 1):
            zustand = self.ledger.milestone(milestone_id)
            if zustand.state in S.TERMINAL_STATES:
                return zustand.state
            if zustand.state in S.PARKED_STATES:
                if zustand.state == S.BLOCKED and zustand.resume_at:
                    if (now or time.time()) < zustand.resume_at:
                        return zustand.state
                    # Der Weckzeitpunkt ist erreicht: erneut versuchen.
                    await self._unblock(milestone_id, now=now)
                    continue
                return zustand.state
            weiter = await self._one_round(milestone_id, nummer, now=now)
            if not weiter:
                break
        return self.ledger.milestone(milestone_id).state

    async def _one_round(self, milestone_id: str, nummer: int, *,
                         now: float = 0.0) -> bool:
        zustand = self.ledger.milestone(milestone_id)
        if await self._contract_proposal(milestone_id, now=now):
            return False
        zustand = self.ledger.milestone(milestone_id)
        vorher = zustand.state
        if vorher in (S.PLANNING, S.BUILDING, S.FIXING):
            ok = await self._build_phase(milestone_id, now=now)
            if not ok:
                self.rounds.append(Round(nummer, vorher,
                                         self.ledger.milestone(milestone_id).state,
                                         note="Bauphase endete ohne Fortschritt"))
                return False
            vorher = self.ledger.milestone(milestone_id).state
        if vorher == S.TESTING:
            await self._test_phase(milestone_id, now=now)
            vorher = self.ledger.milestone(milestone_id).state
        if vorher == S.REVIEWING:
            await self._review_phase(milestone_id, nummer, now=now)
        nachher = self.ledger.milestone(milestone_id).state
        self.rounds.append(Round(nummer, zustand.state, nachher))
        return nachher not in (S.TERMINAL_STATES | S.PARKED_STATES)

    # -- Bauen ---------------------------------------------------------------
    async def _build_phase(self, milestone_id: str, *, now: float = 0.0) -> bool:
        zustand = self.ledger.milestone(milestone_id)
        if zustand.state == S.PLANNING:
            # PLANNING ist kein Bauzustand. Die Kantentabelle kennt keinen Weg
            # von dort nach TESTING, und das ist richtig: wer plant, hat noch
            # nichts gebaut, das man messen koennte.
            M.transition(self.ledger, milestone_id, S.BUILDING,
                         summary="Planung abgeschlossen", now=now)
            zustand = self.ledger.milestone(milestone_id)
        lage = await (self.preflight or CAP.preflight)(self.ledger, milestone_id, self.adapters,
                                   now=now)
        optionen = lage["builder_options"]
        aufgabe = zustand.current_task or json.loads(
            zustand.contract_json)["objective"]
        groesse = B.MEDIUM

        # MODEL_FIT vor EXECUTION_FIT vor ROUTE — die Reihenfolge ist der
        # Punkt: wer zuerst schaut, was verfuegbar ist, bekommt eine
        # Praeferenz, die genau das bevorzugt, was gerade laeuft.
        anlass = (RT.NEW_PHASE if zustand.state == S.BUILDING
                  else RT.NEW_TASK)
        route = RT.decide(trigger=anlass, task_kind=(
            "repair" if zustand.state == S.FIXING else "implementation"),
            adapters=self.adapters, options=optionen, size=groesse)
        self.ledger.record_route(milestone_id, route, now=now)

        versucht: list[str] = []
        while True:
            # **Die Route wird befolgt, nicht nur gebucht.**
            #
            # Live gemessen in der V0.6-Abnahme: die Route sagte
            # `actual = codex`, und gebaut hat Claude — weil die Wahl hier an
            # `choose_builder` hing, das alphabetisch sortiert. In V0.5 fiel
            # das nie auf: es gab genau einen Schreiber, und beide Wege kamen
            # zum selben Namen. Mit zwei Schreibern ist eine gebuchte Route,
            # der niemand folgt, schlimmer als keine — sie steht als
            # Begruendung im Buch fuer etwas, das nicht geschah.
            #
            # `choose_builder` bleibt der Rueckfall: fuer den zweiten Anlauf
            # nach einem Kontingentausfall, und wenn die Route ins Leere
            # zeigt.
            name = ""
            if route.route and route.route not in versucht:
                bericht = optionen.get(route.route)
                if bericht is not None and bericht.accepts(groesse):
                    name = route.route
            if not name:
                name = CAP.choose_builder(optionen, size=groesse,
                                          exclude=tuple(versucht))
            if not name:
                await self._block(milestone_id, "capacity",
                                  "kein zugelassener Builder verfuegbar", now=now)
                return False
            versucht.append(name)
            adapter = self.adapters[name]
            basis = EV.head_commit(self.workspace)
            paket = CTX.compile_context(self.ledger, milestone_id,
                                        next_task=aufgabe)
            phase_id = self.ledger.start_phase(
                milestone_id, kind="build", builder=name, base_commit=basis,
                attempt_digest=self._digest(milestone_id, name, aufgabe), now=now)
            self.ledger.set_fields(milestone_id, builder=name,
                                   current_task=aufgabe[:300], base_commit=basis,
                                   now=now)

            ergebnis = await adapter.build(
                B.BuildTask(milestone_id, aufgabe, size=groesse,
                            context=paket.text), self.workspace)
            if ergebnis.pgid:
                self.ledger.set_phase_child(phase_id, pgid=ergebnis.pgid,
                                            started_at=ergebnis.started_at,
                                            executable=ergebnis.executable)

            if ergebnis.quota:
                # Kontingent ist KEIN Reparaturversuch (Vertrag §8).
                self.ledger.record_usage(milestone_id, role=S.ROLE_BUILDER,
                                         provider=name, note="quota", now=now)
                self.ledger.finish_phase(phase_id, state="quota",
                                         summary="Kontingent erschoepft", now=now)
                self.ledger.record_event(milestone_id, "quota_failover",
                                         f"{name} erschoepft", now=now)
                punkt = B.safe_checkpoint(self.workspace, phase="quota_failover")
                if not self._checkpoint_ok(milestone_id, punkt, now=now):
                    return False
                optionen[name] = CAP.CapacityReport(
                    S.ROLE_BUILDER, name, CAP.EXHAUSTED,
                    {"last_quota_hit_at": now or time.time()}, now or time.time())
                # „weg von", nicht „nach": `name` ist der ERSCHOEPFTE.
                # Live gelesen in der V0.6-Abnahme: im Buch stand
                # `builder_switched: nach claude`, waehrend Codex uebernahm —
                # eine Zeile, die genau das Gegenteil dessen sagt, was
                # geschah, und die niemand spaeter richtig liest.
                self.ledger.record_event(milestone_id, "builder_switched",
                                         f"weg von {name}", now=now)
                continue

            if ergebnis.outcome in (B.UNAVAILABLE, B.REFUSED):
                self.ledger.finish_phase(phase_id, state=ergebnis.outcome,
                                         summary=ergebnis.detail, now=now)
                optionen[name] = CAP.CapacityReport(
                    S.ROLE_BUILDER, name, CAP.UNAVAILABLE,
                    {"grund": ergebnis.detail}, now or time.time())
                continue

            self.ledger.record_usage(milestone_id, role=S.ROLE_BUILDER,
                                     provider=name, wall_seconds=ergebnis.elapsed,
                                     provider_tokens=ergebnis.provider_tokens,
                                     note="ok" if ergebnis.ok else "failed",
                                     now=now)
            punkt = B.safe_checkpoint(self.workspace, phase="build")
            if not self._checkpoint_ok(milestone_id, punkt, now=now):
                self.ledger.finish_phase(phase_id, state="failed",
                                         summary="Checkpoint verweigert", now=now)
                return False
            self.ledger.finish_phase(
                phase_id, state="succeeded" if ergebnis.ok else "failed",
                summary=(ergebnis.text or ergebnis.detail)[:600],
                result_commit=punkt.commit, now=now)
            self.ledger.set_fields(milestone_id, last_commit=punkt.commit, now=now)
            EV.record_commit(self.ledger, milestone_id, self.workspace,
                             base=basis, now=now)
            self._publish(milestone_id, punkt.commit, now=now)
            M.transition(self.ledger, milestone_id, S.TESTING,
                         summary=f"gebaut von {name}", now=now)
            return True

    def _publish(self, milestone_id: str, commit: str, *,
                 now: float = 0.0) -> None:
        """Den Checkpoint im kanonischen Speicher erreichbar machen (DEBT-0179).

        Ohne diesen Schritt kann das Offsite-Herkunftstor den Commit im
        Buendel nicht finden — der Arbeitsbereich ist ein isolierter Klon, und
        dessen Commits existieren nirgendwo sonst.

        Ein Fehlschlag hier bricht den Lauf NICHT ab: er wird als Finding
        gefuehrt. Der Grund ist Ehrlichkeit ueber die Folge — ohne Publikation
        wird das Gate rot, und das soll es dann auch, statt dass ein
        Nebenschritt den ganzen Milestone kippt.
        """
        if self.publisher is None:
            return
        zustand = self.ledger.milestone(milestone_id)
        try:
            veroeffentlicht = self.publisher.publish(
                clone=self.workspace, commit=commit,
                milestone_id=milestone_id,
                contract_hash=zustand.contract_hash,
                expected_contract_hash=zustand.contract_hash, now=now)
        except PUB.PublishRefused as exc:
            schwere = ("blocker" if exc.reason == "credential_in_checkpoint"
                       else "major")
            self.ledger.open_finding(
                milestone_id, severity=schwere,
                origin=("security" if schwere == "blocker" else "publisher"),
                title=f"Checkpoint nicht veroeffentlicht ({exc.reason})",
                detail=exc.detail[:1_000], now=now)
            log.warning("autopilot.publish_refused", reason=exc.reason)
            return
        self.ledger.record_publication(milestone_id, veroeffentlicht, now=now)

    def _checkpoint_ok(self, milestone_id: str, punkt: B.CheckpointResult, *,
                       now: float = 0.0) -> bool:
        """Ein Kredentialfund haelt den Milestone an — fail closed (Amendment 6)."""
        if punkt.ok:
            return True
        self.ledger.open_finding(
            milestone_id, severity="blocker", origin="security",
            title="Anmeldematerial im Arbeitsbereich",
            detail=("Der Checkpoint wurde verweigert: "
                    + ", ".join(punkt.findings[:10])), now=now)
        M.park(self.ledger, milestone_id, category="FAILED_NEEDS_OWNER",
               kind="policy_refusal",
               question=("Im Arbeitsbereich liegt etwas, das wie Anmeldematerial "
                         "aussieht. Ich habe nichts committet und nichts "
                         "weitergereicht. Bitte sieh nach, bevor es weitergeht."),
               now=now)
        return False

    def _digest(self, milestone_id: str, builder: str, aufgabe: str) -> str:
        from solvio.agent_runtime.budget import attempt_digest
        return attempt_digest("build", builder, aufgabe)

    def failed_attempts(self, milestone_id: str, digest: str) -> int:
        """Wie oft DERSELBE Versuch schon erfolglos war (Vertrag §13).

        Gezaehlt werden ausschliesslich `failed`-Phasen. `quota` steht bewusst
        NICHT darin: ein erschoepftes Kontingent ist kein Reparaturversuch, und
        wer es mitzaehlt, schickt den Milestone nach zwei Kontingentenden zur
        Ursachenanalyse, obwohl niemand etwas falsch gemacht hat.
        """
        if not digest:
            return 0
        return sum(1 for p in self.ledger.phases(milestone_id, limit=200)
                   if p["attempt_digest"] == digest and p["state"] == "failed")

    def loop_detected(self, milestone_id: str, digest: str) -> bool:
        return self.failed_attempts(milestone_id, digest) >= LOOP_THRESHOLD

    # -- Testen --------------------------------------------------------------
    async def _test_phase(self, milestone_id: str, *, now: float = 0.0) -> str:
        phase_id = self.ledger.start_phase(milestone_id, kind="test", now=now)
        beleg, ergebnis, wieder = EV.record_gate(
            self.ledger, milestone_id, self.workspace, python=self.python,
            now=now)
        self.ledger.finish_phase(
            phase_id, state="succeeded" if ergebnis.ok else "failed",
            summary=ergebnis.summary()
            + (" (wiederverwendet)" if wieder else ""), now=now)
        self.ledger.set_fields(milestone_id, current_task="", now=now)

        # **Die unabhaengige Abnahme (DEBT-0234).** Das Gate oben lief als
        # Skript AUS DEM ARBEITSBEREICH — der Gepruefte stellte die Pruefung.
        # Diese zweite Messung faehrt die urspruenglichen Tests des
        # Basis-Commits gegen den neuen Code, in einem Baum, den der Builder
        # nie beruehrt hat. Sie kostet einen zweiten Gate-Lauf und wird
        # deshalb nur gefahren, wenn der erste ueberhaupt gruen ist: ein rotes
        # Arbeitsbereichsgate ist schon ohne sie ein Nein.
        self._independent = ""
        if ergebnis.ok:
            try:
                self._independent, _unabh, _befund = IND.measure(
                    self.ledger, milestone_id, self.workspace,
                    python=self.python, now=now)
            except (IND.IndependentError, EV.EvidenceError, OSError) as exc:
                # Ein Fehlschlag der Abnahme ist ein Befund, kein Nichts. Er
                # laesst `self._independent` leer — und damit bleibt jedes
                # DETERMINISTIC-Gate-Kriterium unbewiesen.
                self.ledger.open_finding(
                    milestone_id, severity="blocker",
                    title="Die unabhaengige Abnahme fand nicht statt",
                    detail=f"{type(exc).__name__}: {str(exc)[:400]}",
                    origin="core", now=now)
                log.warning("autopilot.independent_failed",
                            milestone=milestone_id, kind=type(exc).__name__)

        M.transition(self.ledger, milestone_id, S.REVIEWING,
                     summary=ergebnis.summary(), gate_evidence_id=beleg, now=now)
        return beleg

    # -- Beurteilen ----------------------------------------------------------
    async def _review_phase(self, milestone_id: str, runde: int, *,
                            now: float = 0.0) -> None:
        zustand = self.ledger.milestone(milestone_id)
        kriterien = self.ledger.criteria(milestone_id)
        findings = self.ledger.findings(milestone_id, open_only=True)
        beleg = self._latest_gate(milestone_id, zustand.last_commit)
        gate = self.ledger.evidence(beleg) if beleg else None

        phase_id = self.ledger.start_phase(milestone_id, kind="review", now=now)
        # Die Belege, die der Lead zitieren DARF — und nur die. Ohne sie
        # muesste er raten, und `validate()` verwirft jedes geratene Urteil.
        belege = [e for e in (beleg,) if e]
        for eintrag in self.ledger.events(milestone_id, limit=60):
            if (eintrag["kind"] == "evidence_recorded" and eintrag["ref"]
                    and eintrag["ref"] not in belege):
                belege.append(eintrag["ref"])
            if len(belege) >= 8:
                break
        paket = CTX.compile_context(
            self.ledger, milestone_id,
            diff=EV.diff_text(self.workspace, zustand.base_commit),
            gate_summary=(gate.summary if gate else "keine Messung"),
            evidence=belege)

        # Die Schleifenbremse entscheidet ueber die Modellstufe, nicht die
        # Rundenzahl allein: wer zweimal dasselbe erfolglos versucht hat,
        # braucht eine andere Sicht, nicht einen weiteren Anlauf.
        letzter = (self.ledger.phases(milestone_id, limit=1) or [{}])
        digest = ""
        for p in self.ledger.phases(milestone_id, limit=200):
            if p["kind"] == "build" and p["attempt_digest"]:
                digest = p["attempt_digest"]
                break
        schleife = self.loop_detected(milestone_id, digest)
        if schleife:
            self.ledger.record_event(
                milestone_id, "loop_detected",
                f"{self.failed_attempts(milestone_id, digest)} gleichartige "
                f"Versuche erfolglos", ref=digest, now=now)
        stufe = LEAD.TIER_LARGE if (schleife or runde >= 3) else LEAD.TIER_SMALL
        try:
            urteil = await self.lead.judge(
                context=paket.text,
                allowed_builders=B.writers(self.adapters),
                open_finding_ids={f.finding_id for f in findings},
                criterion_keys={k["key"] for k in kriterien},
                evidence_ids=set(belege),
                deterministic_keys={k["key"] for k in kriterien
                                    if k["evidence_type"] == "DETERMINISTIC"},
                tier=stufe)
        except LEAD.LeadRefused as exc:
            self.ledger.finish_phase(phase_id, state="failed",
                                     summary=f"{exc.reason}", now=now)
            if exc.reason in ("lead_unreachable", "no_broker"):
                # Der Lead ist nicht ansprechbar — das ist eine Lage, kein
                # Urteil. Warten und wieder versuchen.
                await self._block(milestone_id, "capacity",
                                  f"Technical Lead: {exc.reason}", now=now)
                return
            # Ein UNGUELTIGES Urteil ist keine Kapazitaetsfrage. Es ist ein
            # Befund ueber den Lead, und er gehoert ins Buch — nicht in eine
            # Wartschleife, die so aussieht, als fehle ein Kontingent.
            self.ledger.open_finding(
                milestone_id, severity="major", origin="lead",
                title=f"Der Technical Lead lieferte kein gueltiges Urteil "
                      f"({exc.reason})",
                detail=exc.detail[:1_000], now=now)
            M.transition(self.ledger, milestone_id, S.FIXING,
                         summary=f"Urteil verworfen: {exc.reason}", now=now)
            return

        self.ledger.record_usage(milestone_id, role=S.ROLE_LEAD,
                                 provider=getattr(self.lead, "provider", "provider-broker"), model=urteil.model,
                                 provider_tokens=urteil.tokens, note="ok", now=now)
        self._apply(milestone_id, urteil, gate_evidence=beleg, now=now)
        self.ledger.record_decision(milestone_id, role=S.ROLE_LEAD,
                                    verdict=urteil.verdict, model=urteil.model,
                                    next_action=urteil.next_action,
                                    rationale=urteil.rationale, now=now)
        self.ledger.finish_phase(phase_id, state="succeeded",
                                 summary=f"{urteil.verdict}: {urteil.rationale}"[:600],
                                 now=now)
        CTX.build_handoff(self.ledger, milestone_id, state_after=S.REVIEWING,
                          builder=zustand.builder, model=urteil.model,
                          gate=(json.loads(gate.payload_json) if gate else {}),
                          evidence=[beleg] if beleg else [],
                          recommended_next_action=urteil.next_action)
        await self._act(milestone_id, urteil, gate_evidence=beleg,
                        loop=schleife, now=now)

    def _latest_gate(self, milestone_id: str, commit: str) -> str:
        beleg = self.ledger.fresh_evidence(
            milestone_id, kind="test_report", commit=commit,
            env_fingerprint=EV.environment_fingerprint(self.workspace,
                                                       python=self.python))
        return beleg.evidence_id if beleg else ""

    def _apply(self, milestone_id: str, urteil: LEAD.Verdict, *,
               gate_evidence: str, now: float = 0.0) -> None:
        """Was der Lead entschieden hat, im Ledger — aber nur, was er darf."""
        for f in urteil.findings:
            self.ledger.open_finding(milestone_id, severity=f["severity"],
                                     title=f["title"], detail=f.get("detail", ""),
                                     origin="lead", now=now)
        for fid in urteil.close_findings:
            self.ledger.close_finding(fid, now=now)
        for eintrag in urteil.proven:
            self.ledger.set_criterion(milestone_id, eintrag["key"],
                                      state=S.CRIT_PROVEN,
                                      evidence_ref=eintrag["evidence_ref"], now=now)
        # DETERMINISTIC-Kriterien setzt ausschliesslich die Messung.
        gate = self.ledger.evidence(gate_evidence) if gate_evidence else None
        # **Beweiskraft hat die unabhaengige Messung, nicht die des
        # Arbeitsbereichs (DEBT-0234).** Beide muessen gruen sein: das
        # Arbeitsbereichsgate, weil es die NEUEN Tests kennt, und die
        # unabhaengige Abnahme, weil sie die urspruenglichen benutzt. Fehlt
        # die zweite oder hat sie verweigert, wird nichts bewiesen — eine
        # nicht stattgefundene Abnahme ist keine bestandene.
        unabhaengig = (self.ledger.evidence(self._independent)
                       if getattr(self, "_independent", "") else None)
        if gate is not None and gate.ok and unabhaengig is not None and unabhaengig.ok:
            for k in self.ledger.criteria(milestone_id):
                if (k["evidence_type"] == "DETERMINISTIC"
                        and k["state"] != S.CRIT_PROVEN
                        and self._gate_proves(k["key"])):
                    self.ledger.set_criterion(milestone_id, k["key"],
                                              state=S.CRIT_PROVEN,
                                              evidence_ref=self._independent,
                                              now=now)

    def _nothing_left_to_do(self, milestone_id: str, gate_evidence: str) -> bool:
        """Ist die Lage messbar fertig? Drei Messungen, kein Urteil.

        Bewusst OHNE das Urteil des Leads: diese Frage soll beantworten, ob
        noch etwas offen IST — nicht, ob jemand meint, es sei etwas offen.
        """
        pruefung = M.ready_check(self.ledger, milestone_id,
                                 gate_evidence_id=gate_evidence,
                                 lead_verdict=LEAD.READY)
        return bool(pruefung.gate_green
                    and pruefung.acceptance_total > 0
                    and pruefung.acceptance_proven == pruefung.acceptance_total
                    and not pruefung.blocking_findings)

    @staticmethod
    def _gate_proves(key: str) -> bool:
        """Welche DETERMINISTIC-Kriterien das Gate selbst beweist.

        Bewusst eng: nur Kriterien, die woertlich das Gate meinen. Alles andere
        braucht seine eigene Messung — ein gruenes Gate beweist nicht jede
        Behauptung, die jemand DETERMINISTIC genannt hat.
        """
        return key in ("gate", "gate_green", "tests", "test_gate")

    async def _act(self, milestone_id: str, urteil: LEAD.Verdict, *,
                   gate_evidence: str, loop: bool = False,
                   now: float = 0.0) -> None:
        if urteil.verdict == LEAD.READY:
            try:
                M.transition(self.ledger, milestone_id, S.READY,
                             gate_evidence_id=gate_evidence,
                             lead_verdict=LEAD.READY, summary=urteil.rationale,
                             now=now)
                return
            except M.TransitionRefused as exc:
                # Der Lead wollte READY, die Bedingungen tragen nicht. Das ist
                # kein Fehler des Leads, sondern der Zweck der Pruefung.
                self.ledger.record_event(milestone_id, "decision_recorded",
                                         f"READY verweigert: {exc.detail}", now=now)
                urteil = LEAD.Verdict(verdict=LEAD.FIX, next_action="fix",
                                      rationale=f"READY verweigert: {exc.detail}",
                                      task=urteil.task or "Bedingungen erfuellen")
        # -- Ein FIX ohne Substanz haelt einen fertigen Milestone nicht fest -
        #
        # Live gemessen in der B5-Abnahme: Gate gruen, 3 von 3 Kriterien
        # `proven`, alle Findings geschlossen — und der Lead sagte trotzdem
        # FIX, ohne einen einzigen neuen Befund zu nennen. Der Milestone waere
        # so bis zur Rundengrenze gelaufen und haette nichts mehr gefunden.
        #
        # **Das ist ausdruecklich NICHT die Regel „gruenes Gate = READY".** Es
        # muessen VIER Dinge zugleich gelten, und drei davon sind Messungen:
        # das Gate ist gruen, JEDES Kriterium ist bewiesen, es steht kein
        # blockierendes Finding offen — und der Lead hat in diesem Urteil
        # keinen neuen Befund beigetragen. Nennt er einen, geht es nach
        # FIXING wie bisher; der Lead bleibt echter Beurteiler.
        if (urteil.verdict in (LEAD.FIX, LEAD.BUILD) and not urteil.findings
                and self._nothing_left_to_do(milestone_id, gate_evidence)):
            self.ledger.record_event(
                milestone_id, "decision_recorded",
                f"{urteil.verdict} ohne neuen Befund bei gruenem Gate und "
                f"vollstaendiger Akzeptanz — der Milestone ist fertig",
                now=now)
            try:
                M.transition(self.ledger, milestone_id, S.READY,
                             summary=("fertig: gruen, alles bewiesen, kein "
                                      "offener Befund"),
                             gate_evidence_id=gate_evidence,
                             lead_verdict=LEAD.READY, now=now)
                return
            except M.TransitionRefused as exc:
                # Die Pruefung hat das letzte Wort. Wenn sie nein sagt, war
                # die Lage doch nicht fertig — dann gilt das Urteil des Leads.
                self.ledger.record_event(
                    milestone_id, "decision_recorded",
                    f"READY verweigert: {exc.detail}", now=now)

        if urteil.verdict == LEAD.NEEDS_HUMAN:
            M.park(self.ledger, milestone_id, category="DECISION_REQUIRED",
                   kind="product_decision",
                   question=urteil.rationale or "Es braucht deine Entscheidung.",
                   now=now)
            return
        if loop and urteil.verdict == LEAD.FIX and urteil.next_action == "fix":
            # Der normale Retry ist zu Ende (Vertrag §13). Ein weiteres „mach
            # es nochmal" waere genau die Schleife, die hier gebrochen werden
            # soll — der Lead muss aus dem Katalog waehlen.
            self.ledger.record_event(
                milestone_id, "loop_detected",
                "erneutes FIX nach zwei erfolglosen Versuchen — der Lead muss "
                "aus dem Katalog waehlen", now=now)
            M.park(self.ledger, milestone_id, category="FAILED_NEEDS_OWNER",
                   kind="policy_refusal",
                   question=("Zweimal derselbe Reparaturversuch, zweimal "
                             "erfolglos, und der Technical Lead schlaegt "
                             "wieder dasselbe vor. Ich halte an, statt es ein "
                             "drittes Mal zu versuchen."), now=now)
            return
        ziel = S.FIXING if urteil.verdict in (LEAD.FIX, LEAD.ESCALATE) else S.BUILDING
        self.ledger.set_fields(milestone_id,
                               current_task=(urteil.task or urteil.rationale)[:300],
                               now=now)
        M.transition(self.ledger, milestone_id, ziel, summary=urteil.rationale,
                     now=now)

    # -- Parken --------------------------------------------------------------
    async def _contract_proposal(self, milestone_id: str, *,
                                 now: float = 0.0) -> bool:
        """Ein Contract-Vorschlag im Arbeitsbaum ist eine Grenze, kein Diff.

        Der Context sagt jedem Builder: „READ-ONLY. Eine Aenderung im
        Arbeitsbaum ist ein Vorschlag und erzeugt CONTRACT_CHANGE_REQUIRED."
        Diese Zusage war bis zur A7-Abnahme leer — nichts sah nach. Ein
        angekuendigtes Tor, durch das jeder unbemerkt geht, ist schlimmer als
        keines: es steht in der Dokumentation als Sicherheit.

        Geprueft wird der Hash, nicht Feld fuer Feld. Was der Vorschlag WERT
        ist, entscheidet der Eigentuemer an der Grenze — nicht diese Zeile.
        """
        pfad = os.path.join(self.workspace, PROPOSAL_FILE)
        if not os.path.isfile(pfad):
            return False
        zustand = self.ledger.milestone(milestone_id)
        if zustand.state in (S.PARKED_STATES | S.TERMINAL_STATES):
            return False
        try:
            with open(pfad, encoding="utf-8") as fh:
                vorschlag = json.load(fh)
            kanonisch = C.parse(json.loads(zustand.contract_json))
            grund = C.differs(kanonisch, vorschlag)
        except (OSError, ValueError, C.ContractError) as exc:
            # Unlesbar ist NICHT „keine Aenderung". Wer den Vorschlag nicht
            # lesen kann, weiss auch nicht, dass er harmlos ist.
            grund = f"unreadable_proposal:{type(exc).__name__}"
        if not grund:
            return False
        M.park(self.ledger, milestone_id, category="DECISION_REQUIRED",
               kind="contract_change",
               question=("Im Arbeitsbaum liegt ein Contract-Vorschlag "
                         f"({grund}). Der kanonische Contract bleibt gueltig, "
                         "bis du entscheidest."), now=now)
        log.warning("autopilot.contract_proposal", milestone=milestone_id,
                    reason=grund)
        return True

    async def _block(self, milestone_id: str, grund: str, text: str, *,
                     now: float = 0.0) -> None:
        M.transition(self.ledger, milestone_id, S.BLOCKED, block_reason=grund,
                     resume_at=(now or time.time()) + CAP.QUOTA_COOLDOWN,
                     summary=text, now=now)
        log.warning("autopilot.blocked", milestone=milestone_id, reason=grund)

    async def _unblock(self, milestone_id: str, *, now: float = 0.0) -> None:
        zustand = self.ledger.milestone(milestone_id)
        ziel = zustand.state_before_park or S.BUILDING
        M.transition(self.ledger, milestone_id, ziel,
                     summary="Weckzeitpunkt erreicht", now=now)
