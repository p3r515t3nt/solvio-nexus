#!/usr/bin/env python3
"""Den Entwicklungs-Autopiloten bedienen — oertlich, ohne Dashboard.

    python3 scripts/autopilot.py start   --contract <datei.json> --workspace <pfad>
    python3 scripts/autopilot.py status  [--milestone <id>] [--json]
    python3 scripts/autopilot.py run     --milestone <id> --workspace <pfad>
    python3 scripts/autopilot.py answer  --boundary <id> --text "..."
    python3 scripts/autopilot.py stop    --milestone <id>

`start` legt den Milestone an und PINNT den Contract. Ab da ist der Ledger die
Quelle; die Datei ist bestenfalls noch ein Vorschlag.

`answer` ist der Weg zurueck aus einer Nutzergrenze. Er beantwortet genau eine
Frage und setzt den Milestone dorthin zurueck, wo er herkam — nicht irgendwohin.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from solvio.autopilot import contract as C      # noqa: E402
from solvio.autopilot import driver as D        # noqa: E402
from solvio.autopilot import machine as M       # noqa: E402
from solvio.autopilot import status as ST       # noqa: E402
from solvio.autopilot import store as S         # noqa: E402


def _say(text: str = "") -> None:
    print(text, flush=True)


def cmd_start(args) -> int:
    vertrag = C.load(args.contract)
    led = S.AutopilotLedger()
    try:
        milestone = led.create_milestone(vertrag, repository=args.workspace or "")
    except S.LedgerError as exc:
        _say(f"Nicht angelegt: {exc.reason} ({exc.detail})")
        return 1
    _say(f"Milestone {milestone.milestone_id} angelegt.")
    _say(f"  Contract {milestone.contract_version}, {milestone.contract_hash}")
    _say(f"  Akzeptanzkriterien: {led.acceptance_counts(milestone.milestone_id)[1]}")
    _say()
    _say("Der Contract liegt jetzt im Ledger. Eine Aenderung der Datei ist ab")
    _say("hier ein Vorschlag und wird nie still uebernommen.")
    return 0


def cmd_status(args) -> int:
    led = S.AutopilotLedger()
    if args.milestone:
        namen = [args.milestone]
    else:
        namen = [m.milestone_id for m in led.milestones()]
    if not namen:
        _say("Kein Milestone im Buch.")
        return 0
    berichte = []
    for name in namen:
        try:
            berichte.append(ST.snapshot(led, name))
        except S.LedgerError as exc:
            _say(f"{name}: {exc.reason}")
            return 1
    if args.json:
        _say(json.dumps(berichte if len(berichte) > 1 else berichte[0],
                        indent=2, ensure_ascii=False))
        return 0
    for b in berichte:
        _say(f"{b['milestone']}  [{b['state']}]"
             + (f" ({b['block_reason']})" if b["block_reason"] else ""))
        _say(f"  Akzeptanz : {b['acceptance_proven']}/{b['acceptance_total']} bewiesen")
        if b["last_gate"]:
            marke = "gruen" if b["last_gate"]["ok"] else "ROT"
            _say(f"  Gate      : {marke} — {b['last_gate']['summary']}")
        else:
            _say("  Gate      : noch nicht gemessen")
        _say(f"  Builder   : {b['builder'] or '—'}   Commit: {b['last_commit'] or '—'}")
        if b["current_task"]:
            _say(f"  Aufgabe   : {b['current_task']}")
        if b["open_findings"]:
            _say(f"  Findings  : {len(b['open_findings'])} offen")
            for f in b["open_findings"][:5]:
                _say(f"              [{f['severity']}] {f['id']}: {f['title']}")
        if b["human_required"]:
            _say("  WARTET AUF DICH:")
            for g in b["human_required"]:
                _say(f"    {g['id']} ({g['category']}/{g['kind']})")
                _say(f"    {g['question']}")
                _say(f"    -> python3 scripts/autopilot.py answer "
                     f"--boundary {g['id']} --text \"...\"")
        if b["token_usage"]:
            teile = [f"{rolle}: {w['calls']} Aufrufe"
                     + (f", {w['provider_tokens']} Token" if w["provider_tokens"] else "")
                     for rolle, w in sorted(b["token_usage"].items())]
            _say(f"  Verbrauch : {' | '.join(teile)}")
        _say()
    return 0


def cmd_answer(args) -> int:
    led = S.AutopilotLedger()
    try:
        milestone_id = led.resolve_boundary(args.boundary, args.text)
    except S.LedgerError as exc:
        _say(f"Nicht beantwortet: {exc.reason} ({exc.detail})")
        return 1
    zustand = led.milestone(milestone_id)
    ziel = zustand.state_before_park
    _say(f"Grenze {args.boundary} beantwortet.")
    if zustand.state == S.HUMAN_REQUIRED and not led.open_boundaries(milestone_id):
        try:
            M.transition(led, milestone_id, ziel)
            _say(f"{milestone_id} laeuft weiter bei {ziel}.")
        except M.TransitionRefused as exc:
            _say(f"Noch nicht fortgesetzt: {exc.reason} ({exc.detail})")
            return 1
    else:
        offen = len(led.open_boundaries(milestone_id))
        _say(f"{offen} weitere Frage(n) offen — noch kein Fortlauf.")
    return 0


def cmd_run(args) -> int:
    led = S.AutopilotLedger()
    try:
        sperre = D._open_lock()
    except D.DriverLocked as exc:
        _say(f"Ein anderer Treiber laeuft bereits: {exc}")
        return 1
    try:
        fahrer = D.Driver(led, workspace=args.workspace)
        from solvio.autopilot import lead as LEAD
        from solvio.autopilot import publisher as PUB
        fahrer.lead = LEAD.TechnicalLead()
        # Ohne den Veroeffentlicher bleibt jeder Checkpoint im isolierten Klon,
        # und das Offsite-Herkunftstor findet den ausfuehrenden Commit nicht im
        # Buendel: 18 rote Zusicherungen in `test_offsite_health.py`, alle mit
        # demselben `provenance_unproven/commit_absent`. Gemessen am
        # 2026-09-03 — der Treiber konnte es die ganze Zeit, das Bedienskript
        # hat es ihm nie gesagt (DEBT-0179 baute die Naht, niemand hat sie hier
        # angeschlossen).
        fahrer.publisher = PUB.CheckpointPublisher()
        zustand = asyncio.run(fahrer.run(args.milestone, max_rounds=args.rounds))
    finally:
        sperre.close()
    _say(f"{args.milestone} steht auf {zustand}.")
    return 0 if zustand in (S.READY,) else 1


def cmd_stop(args) -> int:
    led = S.AutopilotLedger()
    try:
        M.transition(led, args.milestone, S.STOPPED, summary="vom Eigentuemer")
    except M.TransitionRefused as exc:
        _say(f"Nicht gestoppt: {exc.reason} ({exc.detail})")
        return 1
    _say(f"{args.milestone} gestoppt. Nichts wurde zurueckgenommen.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="autopilot")
    unter = parser.add_subparsers(dest="befehl", required=True)

    p = unter.add_parser("start", help="Milestone anlegen und Contract pinnen")
    p.add_argument("--contract", required=True)
    p.add_argument("--workspace", default="")
    p.set_defaults(func=cmd_start)

    p = unter.add_parser("status", help="Zustand, ohne erfundene Prozente")
    p.add_argument("--milestone", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = unter.add_parser("run", help="den Zyklus drehen")
    p.add_argument("--milestone", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--rounds", type=int, default=D.MAX_ROUNDS)
    p.set_defaults(func=cmd_run)

    p = unter.add_parser("answer", help="eine Nutzergrenze beantworten")
    p.add_argument("--boundary", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_answer)

    p = unter.add_parser("stop", help="den Milestone anhalten")
    p.add_argument("--milestone", required=True)
    p.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
