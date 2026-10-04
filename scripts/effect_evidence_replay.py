#!/usr/bin/env python3
"""Der Notizauftrag noch einmal — mit dem BEREITS GEBAUTEN Artefakt.

**Warum es dieses Skript gibt.** Der Endzustand des echten Laufs war `FAILED`,
und die Reparatur daran (ersetzter Versuch, Ausfuehrungsbeleg) ist in
`tests/test_autonomous_gap_closure.py` mit gestellten Schrittzeilen belegt.
Gestellt heisst: die Zeilen sagen, was der echte Lauf getan HABEN SOLL. Dieses
Skript nimmt stattdessen den Code, den der echte Builder wirklich erzeugt hat,
laesst ihn wirklich laufen und misst, was danach im Buch steht.

**Kein neuer Bau, kein Modell, kein Netz.** Die Faehigkeit wird aus dem
Autopilot-Checkpoint des vorhandenen Laufs geholt — dem Ergebnis, das schon
bezahlt ist. Der Rest ist der normale Core-Weg.

**Was es beweist, wenn es gruen ist.** Der erste Versuch scheitert an der
fehlenden Faehigkeit (`unknown_capability`), der zweite gelingt mit dem echten
Code, der Core misst den Effekt selbst nach, und der Informationsvertrag
schliesst den Auftrag ab. Was es NICHT beweist: dass ein Modell den Plan mit
`erfuellt` fuellt — das ist eine Modellfrage und steht separat.

Aufruf:

    SOLVIO_STATE_DIR=/tmp/solvio-replay \\
      .venv/bin/python scripts/effect_evidence_replay.py [<checkpoint-ref>]
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

#: Der Checkpoint des echten Laufs. Er traegt `solvio_note.py` und den
#: Faehigkeitsvertrag — genau das, was der Builder am 06.09. erzeugte.
STANDARD_REF = "refs/autopilot/gap-1f983ef8cf3ed38c/cp-20260906t005202-28b447"

NOTIZ = "Zahnarzt Dienstag 9 Uhr"


def _artefakt(ref: str) -> tuple[str, str]:
    """Den gebauten Code aus dem Objektspeicher holen. Nur lesen."""
    wurzel = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, check=True,
                            cwd=os.path.dirname(os.path.abspath(__file__))
                            ).stdout.strip()
    commit = subprocess.run(["git", "--git-dir", wurzel, "rev-parse", ref],
                            capture_output=True, text=True, check=True).stdout.strip()
    quelle = subprocess.run(["git", "--git-dir", wurzel, "show",
                             f"{commit}:solvio_note.py"],
                            capture_output=True, text=True, check=True).stdout
    vertrag = subprocess.run(["git", "--git-dir", wurzel, "show",
                              f"{commit}:capability_contract.json"],
                             capture_output=True, text=True, check=True).stdout
    return commit, (quelle, vertrag)


def _lade(quelle: str):
    """Den Builder-Code ausfuehren und seinen Handler zurueckgeben."""
    raum: dict = {}
    exec(compile(quelle, "<solvio_note.py aus dem Checkpoint>", "exec"), raum)
    return raum["write_note"]


async def main() -> int:
    wurzel = (os.environ.get("SOLVIO_STATE_DIR") or "").strip()
    if not wurzel:
        raise SystemExit("SOLVIO_STATE_DIR fehlt — ohne eigenen Zustand nicht.")
    echt = os.path.realpath(os.path.expanduser(wurzel))
    produktiv = os.path.realpath(os.path.expanduser("~/.solvio"))
    if echt == produktiv or echt.startswith(produktiv + os.sep):
        raise SystemExit(f"SOLVIO_STATE_DIR zeigt in den produktiven Zustand: {echt}")
    os.makedirs(echt, mode=0o700, exist_ok=True)
    os.environ.setdefault("SOLVIO_AGENT_RUNS_DB",
                          os.path.join(echt, "agent_runs.sqlite3"))

    ref = sys.argv[1] if len(sys.argv) > 1 else STANDARD_REF
    commit, (quelle, vertrag) = _artefakt(ref)
    schreiber = _lade(quelle)
    print(f"Artefakt:   {ref}")
    print(f"Commit:     {commit[:12]}")
    print(f"Vertrag:    {list(json.loads(vertrag)['capabilities'])}")

    from solvio.agent_runtime import completion as CO
    from solvio.agent_runtime import orchestrator as O
    from solvio.agent_runtime import requirements as RQ
    from solvio.agent_runtime import store as S
    from solvio.agent_runtime import budget as BU

    ledger = S.AgentRunLedger(os.environ["SOLVIO_AGENT_RUNS_DB"])
    orch = O.Orchestrator(ledger=ledger)
    ziel_auftrag = "Schreib mir eine Notiz: Zahnarzt Dienstag 9 Uhr."
    task = ledger.create_task(objective=ziel_auftrag, scope=S.SCOPE_RESEARCH,
                              created_origin="local_owner", created_principal="owner")
    run = ledger.create_run(task_id=task.task_id)
    kontext = O.RunContext(run_id=run.run_id, task_id=task.task_id,
                           scope=S.SCOPE_RESEARCH,
                           ledger=BU.BudgetLedger(budget=BU.DEFAULTS["research"]))
    wirkungen = os.path.join(echt, O.Orchestrator.EFFECT_DIR)
    os.makedirs(wirkungen, mode=0o700, exist_ok=True)
    datei = os.path.join(wirkungen, "notizen.md")
    with open(datei, "w", encoding="utf-8") as fh:
        fh.write("# Notizen\n")

    class Geplant:
        kind, capability, optional = "capability", "note_write", False
        profile = instruction = ""
        arguments = {"pfad": datei, "text": NOTIZ}
        requirement = "h1"

    class Ergebnis:
        state, human_message = "succeeded", "erledigt"
        call_id = "c-replay"
        data = {"pfad": datei}

    # 1. Versuch: die Faehigkeit fehlt — der Router bricht VOR der Ausfuehrung ab.
    erster = ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                attempt=1, capability="note_write")
    ledger.update_step(erster.step_id, state="failed", finished=True,
                       outcome_reason="unknown_capability",
                       summary="Diese Faehigkeit kenne ich nicht.")

    # 2. Versuch: derselbe Schritt, jetzt mit dem GEBAUTEN Code.
    zweiter = ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                 attempt=2, capability="note_write")
    kontext.effect_before = orch._effect_before(Geplant())
    schreiber(datei, NOTIZ)                       # <- der echte Builder-Code
    ledger.update_step(zweiter.step_id, state="succeeded", finished=True,
                       call_id="c-replay", summary="erledigt")
    await orch._record_effect(run, kontext, zweiter, Geplant(), Ergebnis())

    schritte = ledger.steps_for_run(run.run_id)
    ersetzt = orch._superseded(schritte)
    blockierend = [s for s in schritte
                   if s.state not in orch.SETTLED_OK and s.step_id not in ersetzt]
    belege = orch._verified_effects(run.run_id)
    print(f"\nSchritte:   {[(s.seq, s.attempt, s.state, s.outcome_reason) for s in schritte]}")
    print(f"Ersetzt:    {len(ersetzt)}   blockierend: {len(blockierend)}")
    print(f"Belege:     {belege}")

    roh = {"auskunft": [], "handlungen": [{"id": "h1", "text": "Die Notiz anlegen"}],
           "unklar": [], "belege": {"mindestens": 0}}
    bound = RQ.validate(roh, objective=ziel_auftrag)
    beleg = next(iter(belege), "")
    koerper = RQ.snapshot_body([beleg], [])
    urteil = CO.information(
        bound=bound, snapshot=json.loads(koerper),
        snapshot_digest=RQ.snapshot_digest(koerper),
        requirements_digest=RQ.digest_of(bound),
        task_id=task.task_id, run_id=run.run_id, verified_effects=belege,
        judgement={"v": RQ.VERSION, "task_id": task.task_id, "run_id": run.run_id,
                   "anforderungen_digest": RQ.digest_of(bound),
                   "snapshot": RQ.snapshot_digest(koerper),
                   "beantwortet": [{"id": "h1", "belege": [beleg]}],
                   "offen": [], "fehlend": [], "unsicher": [],
                   "weiterarbeit_noetig": False})
    inhalt = open(datei, encoding="utf-8").read()
    print(f"Datei:      {inhalt!r}")
    print(f"Vertrag:    satisfied={urteil.satisfied} reason={urteil.reason!r}")

    gut = (len(ersetzt) == 1 and not blockierend and len(belege) == 1
           and urteil.satisfied and NOTIZ in inhalt)
    print("\n" + ("BELEGT: der Notizauftrag gelingt mit dem gebauten Artefakt."
                  if gut else "NICHT BELEGT."))
    return 0 if gut else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
